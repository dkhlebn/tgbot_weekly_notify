#!/usr/bin/env python3
import html
import json
import os
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Tuple
import pandas as pd
import pytz
import telebot
import gspread
from google.oauth2.service_account import Credentials


class Config:
    def __init__(self, config_path="config.json", teachers_path="teachers.json"):
        self.path = Path(config_path)
        if not self.path.exists():
            raise FileNotFoundError(
                f"Config file not found: {config_path}\n"
                "Create it using config.json.example as template"
            )
        with open(self.path, "r", encoding="utf-8") as f:
            data = json.load(f)

        tg = data["telegram"]
        self.telegram_token = tg["token"]
        self.chat_id = tg["chat_id"]
        self.topic_id = tg["topic_id"]

        gs = data["google_sheets"]
        self.sheet_id = gs["sheet_id"]
        self.credentials_file = gs["credentials_file"]
        self.timezone = pytz.timezone(gs["timezone"])

        sched = data["schedule"]
        self.schedule_day = sched["day_of_week"]
        self.schedule_hour = sched["hour"]
        self.schedule_minute = sched["minute"]

        self.teachers_path = Path(teachers_path)
        if not self.teachers_path.exists():
            raise FileNotFoundError(
                f"Teachers file not found: {teachers_path}\n"
                "Create it using teachers.json.example as template"
            )
        with open(self.teachers_path, "r", encoding="utf-8") as f:
            teachers_data = json.load(f)
        self.teacher_tags: Dict[str, str] = teachers_data.get("teachers", {})


class ReportBot:
    SCOPES = ["https://www.googleapis.com/auth/spreadsheets.readonly"]
    def __init__(self, config: Config):
        self.config = config
        self.bot = telebot.TeleBot(config.telegram_token)


    def get_upcoming_week_reports(self):
        now = datetime.now(self.config.timezone)
        start_of_next_week = (now + timedelta(days=(7 - now.weekday()))).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        end_of_next_week = start_of_next_week + timedelta(days=7)
        print(
            f"🔍 Checking reports between {start_of_next_week.date()} "
            f"and {end_of_next_week.date()}"
        )
        creds = Credentials.from_service_account_file(
            self.config.credentials_file, scopes=self.SCOPES
        )
        gc = gspread.authorize(creds)
        spreadsheet = gc.open_by_key(self.config.sheet_id)
        reports = []
        for worksheet in spreadsheet.worksheets():
            self._process_worksheet(worksheet, start_of_next_week, end_of_next_week, reports)
        return reports


    def _process_worksheet(self, worksheet,
                           start_date, end_date,
                           reports):
        data_rows = worksheet.get_all_values()
        if not data_rows:
            return

        df = pd.DataFrame(data_rows)
        header_idx = -1
        for i, row in df.iterrows():
            if any(str(val).strip() in ["№", "№ недели"] for val in row.values):
                header_idx = i
                break
        if header_idx == -1:
            return
        df.columns = df.iloc[header_idx]
        data = df.iloc[header_idx + 1 :].copy()
        date_col = next((col for col in data.columns if "Дата" in str(col)), None)
        if not date_col:
            return
        data = data[
            data[date_col].notna()
            & (data[date_col] != "Дата")
            & (data[date_col] != "")
        ]
        data[date_col] = pd.to_datetime(
            data[date_col], format="%d.%m.%Y", errors="coerce"
        )
        mask = (
            data[date_col].notna()
            & (data[date_col].dt.date >= start_date.date())
            & (data[date_col].dt.date < end_date.date())
        )
        upcoming = data[mask]
        if not upcoming.empty:
            reports.append((worksheet.title, upcoming))


    def _tag_teacher(self, name: str) -> str:
        if not name or pd.isna(name):
            return "N/A"
        name = str(name).strip()
        if name == "ВСЕ":
            tag = self.config.teacher_tags.get("ВСЕ", "@all")
            return f"ВСЕ ({tag})"
        if ", " in name:
            parts = [p.strip() for p in name.split(",")]
            tagged_parts = [self._tag_teacher(p) for p in parts]
            return ", ".join(tagged_parts)
        tag = self.config.teacher_tags.get(name, None)
        if tag:
            return f"{name} ({tag})"
        else:
            print(f"⚠️ Warning: No Telegram tag found for teacher '{name}'")
            return name


    def format_message(self, reports: List[Tuple[str, pd.DataFrame]]):
        if not reports:
            return "📅 No reports found for the upcoming week."
        msg = "<b>План на новую неделю</b>\n\n"
        for course, df in reports:
            msg += f"<b>📚 {html.escape(course)}</b>\n"
            if course == "Python (ФББ)":
                df = df.copy()
                df["Преподаватель"] = (
                    df["Преподаватель (2/2)"] + ", " + df["Ассистент (1/2)"]
                )
            for _, row in df.iterrows():
                date_val = row.get("Дата")
                date_str = (
                    date_val.strftime("%d.%m (%a)") if pd.notnull(date_val) else "N/A"
                )
                topic = str(row.get("Тема", "No topic")).strip()
                teacher_raw = row.get("Преподаватель", "N/A")
                teacher_tagged = self._tag_teacher(teacher_raw)
                msg += f"🔹 {date_str} | <b>{teacher_tagged}</b>\n"
                msg += f"📝 <i>{html.escape(topic)}</i>\n\n"
            msg += "--------------------------------\n\n"
        return msg.strip()


    def should_send_report(self, now):
        """Check if current time matches scheduled send time."""
        return (
            now.weekday() == self.config.schedule_day
            and now.hour == self.config.schedule_hour
            and now.minute == self.config.schedule_minute
        )


    def send_weekly_report(self):
        """Fetch reports and send to Telegram topic."""
        try:
            reports = self.get_upcoming_week_reports()
            message = self.format_message(reports)
            self.bot.send_message(
                chat_id=self.config.chat_id,
                text=message,
                message_thread_id=self.config.topic_id,
                parse_mode="HTML",
            )
            print(f"✅ Report sent to chat {self.config.chat_id}, topic {self.config.topic_id}")
        except Exception as e:
            print(f"❌ Error sending report: {e}")
            raise

    def run(self):
        print("✅ Bot started. Monitoring schedule...")
        print(f"Current time: {datetime.now(self.config.timezone)}")
        print(
            f"    Schedule: Weekday {self.config.schedule_day} at "
            f"{self.config.schedule_hour:02d}:{self.config.schedule_minute:02d} "
            f"({self.config.timezone})"
        )
        last_checked_minute = None
        while True:
            now = datetime.now(self.config.timezone)
            current_minute = (now.weekday(), now.hour, now.minute)
            if current_minute != last_checked_minute:
                last_checked_minute = current_minute
                if self.should_send_report(now):
                    print(f"⏰ Triggering weekly report at {now}")
                    self.send_weekly_report()
            time.sleep(50)


def main():
    try:
        config = Config()
        bot = ReportBot(config)
        bot.run()
    except KeyboardInterrupt:
        print("\n👋 Bot stopped by user")
    except Exception as e:
        print(f"💥 Fatal error: {e}")
        exit(1)

if __name__ == "__main__":
    main()

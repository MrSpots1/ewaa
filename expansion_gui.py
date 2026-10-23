import argparse
import importlib.util
import queue
import re
import subprocess
import sys
import threading
from datetime import datetime, timedelta
from pathlib import Path

import requests
from PySide6.QtCore import QTimer, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QApplication,
    QGridLayout,
    QGroupBox,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

HERE = Path(__file__).resolve().parent

spec = importlib.util.spec_from_file_location("gen", HERE / "generate_expansions.py")
g = importlib.util.module_from_spec(spec)
spec.loader.exec_module(g)


def notify(title, message):
    try:
        subprocess.run(["notify-send", title, message], timeout=5, check=False)
    except (OSError, subprocess.SubprocessError):
        pass


class Worker(threading.Thread):
    def __init__(self, words, todo, completed, stats, events, stop):
        super().__init__(daemon=True)
        self.words = words
        self.todo = todo
        self.completed = completed
        self.stats = stats
        self.events = events
        self.stop_event = stop

    def run(self):
        try:
            for target in self.todo:
                if self.stop_event.is_set():
                    break
                g.process_word(target, self.completed, self.stats)
                self.events.put(("row", self.stats.recent[-1]))
        finally:
            g.write_final_json(self.completed, self.words)
            self.events.put(("done", None))


class Window(QMainWindow):
    def __init__(self, stats, worker, stop, events, resumed, total):
        super().__init__()
        self.stats = stats
        self.worker = worker
        self.stop_event = stop
        self.events = events
        self.setWindowTitle("EWAA · Acronym Expansion Generator")
        self.resize(1100, 760)

        central = QWidget()
        root = QVBoxLayout(central)

        header = QLabel(
            f"<b>EWAA</b> · model <span style='color:#2a7'>{g.MODEL}</span>"
            f" · thinking {'on' if g.THINK else 'off'}"
            f" · {total - resumed:,} to do · {resumed:,} resumed"
        )
        header.setStyleSheet("font-size: 16px;")
        root.addWidget(header)

        self.bar = QProgressBar()
        self.bar.setRange(0, total)
        self.bar.setValue(resumed)
        self.bar.setFormat("%v / %m words  (%p%)")
        self.bar.setMinimumHeight(28)
        root.addWidget(self.bar)

        stats_box = QGroupBox("Stats")
        grid = QGridLayout(stats_box)
        self.values = {}
        labels = [
            "Success", "Failed", "Below 7", "Retries", "Attempts",
            "Throughput", "Recent pace", "Generation",
            "Elapsed", "ETA", "Finish (est.)", "Remaining",
        ]
        for index, name in enumerate(labels):
            row, col = divmod(index, 2)
            grid.addWidget(QLabel(name + ":"), row, col * 2)
            value = QLabel("—")
            value.setStyleSheet("font-weight: bold;")
            grid.addWidget(value, row, col * 2 + 1)
            self.values[name] = value
        root.addWidget(stats_box)

        self.now_label = QLabel("Idle")
        self.now_label.setWordWrap(True)
        self.now_label.setStyleSheet("padding: 6px; background: #fff7d6;")
        root.addWidget(self.now_label)

        self.table = QTableWidget(0, 5)
        self.table.setHorizontalHeaderLabels(["#", "Word", "Expansion / problem", "Tries", "Time"])
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.verticalHeader().setVisible(False)
        self.table.setEditTriggers(QTableWidget.NoEditTriggers)
        root.addWidget(self.table, stretch=1)

        buttons = QHBoxLayout()
        buttons.addStretch()
        self.stop_button = QPushButton("Stop after current word")
        self.stop_button.clicked.connect(self.request_stop)
        buttons.addWidget(self.stop_button)
        root.addLayout(buttons)

        self.setCentralWidget(central)

        self.timer = QTimer(self)
        self.timer.timeout.connect(self.refresh)
        self.timer.start(250)
        self.finished = False
        self.notified = False

    def request_stop(self):
        self.stop_event.set()
        self.stop_button.setEnabled(False)
        self.stop_button.setText("Stopping...")

    def add_row(self, entry):
        index, target, detail, ok, tries, seconds = entry
        row = 0
        self.table.insertRow(row)
        color = QColor("#1a7f37") if ok else QColor("#cf222e")
        values = [f"{index:,}", target, detail, str(tries), f"{seconds:.1f}s"]
        for col, text in enumerate(values):
            item = QTableWidgetItem(text)
            if col == 2:
                item.setForeground(color)
            self.table.setItem(row, col, item)

    def refresh(self):
        while True:
            try:
                kind, payload = self.events.get_nowait()
            except queue.Empty:
                break
            if kind == "row":
                self.add_row(payload)
            elif kind == "done":
                self.finished = True

        s = self.stats
        done = s.done()
        self.bar.setValue(done)

        rate = s.success / s.processed * 100 if s.processed else 0
        eta = s.eta_seconds()
        pace = s.pace()
        self.values["Success"].setText(f"{s.success:,}  ({rate:.1f}%)")
        self.values["Failed"].setText(f"{s.failed:,}")
        self.values["Below 7"].setText(f"{s.weak:,}")
        self.values["Retries"].setText(f"{s.retries:,}")
        self.values["Attempts"].setText(f"{s.attempts:,}")
        self.values["Throughput"].setText(f"{s.words_per_minute():.1f} words/min")
        self.values["Recent pace"].setText(f"{pace:.1f} s/word" if pace is not None else "—")
        self.values["Generation"].setText(f"{s.tokens_per_second():.1f} tok/s")
        self.values["Elapsed"].setText(g.format_duration(s.elapsed()))
        self.values["ETA"].setText(g.format_duration(eta) if eta is not None else "estimating...")
        if eta is not None:
            finish = (datetime.now() + timedelta(seconds=eta)).strftime("%a %b %d  %H:%M")
        else:
            finish = "—"
        self.values["Finish (est.)"].setText(finish)
        self.values["Remaining"].setText(f"{s.todo - s.processed:,} words")

        if self.finished and not self.notified:
            self.notified = True
            self.now_label.setText(
                f"<b>Finished.</b> {s.success:,} succeeded, {s.failed:,} failed. "
                f"Saved to word_expansions.json"
            )
            self.stop_button.setEnabled(False)
            self.stop_button.setText("Done")
            self.timer.stop()
            notify("EWAA", f"Run finished: {s.success:,} succeeded, {s.failed:,} failed")
        elif s.current is not None:
            problem = f"<br><span style='color:#cf222e'>Last problem: {s.last_error}</span>" if s.last_error else ""
            self.now_label.setText(
                f"<b>Working on:</b> {s.current} &nbsp; attempt {s.current_attempt}/{g.MAX_RETRIES + 1}{problem}"
            )

    def closeEvent(self, event):
        self.stop_event.set()
        self.timer.stop()
        self.worker.join()
        event.accept()


def main():
    parser = argparse.ArgumentParser(description="EWAA expansion generator with a progress window.")
    parser.add_argument("--input", type=Path, default=HERE / "filtered-english-words.txt")
    parser.add_argument("--outdir", type=Path, default=HERE)
    parser.add_argument("--model", default=None)
    args = parser.parse_args()
    if args.model:
        g.MODEL = args.model

    outdir = args.outdir.resolve()
    outdir.mkdir(parents=True, exist_ok=True)
    g.OUTPUT_FILE = outdir / "word_expansions.json"
    g.CHECKPOINT_FILE = outdir / "word_expansions_checkpoint.jsonl"
    g.ERROR_LOG = outdir / "expansion_errors.log"
    g.FAILED_FILE = outdir / "failed_words.txt"

    app = QApplication(sys.argv)

    try:
        requests.get("http://localhost:11434/api/tags", timeout=5).raise_for_status()
    except requests.RequestException:
        QMessageBox.critical(None, "Ollama not reachable", "Start Ollama on localhost:11434 first.")
        return 1

    words = list(dict.fromkeys(
        line.strip() for line in args.input.resolve().open("r", encoding="utf-8")
        if line.strip() and re.fullmatch(r"[A-Za-z]+", line.strip())
    ))
    completed = g.load_completed()
    todo = [word for word in words if word.lower() not in completed]
    resumed = len(words) - len(todo)

    stats = g.RunStats(total=len(words), already_done=resumed, todo=len(todo))
    events = queue.Queue()
    stop = threading.Event()

    if not todo:
        g.write_final_json(completed, words)
        QMessageBox.information(None, "Nothing to do", "All words already have expansions.")
        return 0

    worker = Worker(words, todo, completed, stats, events, stop)
    window = Window(stats, worker, stop, events, resumed, len(words))
    worker.start()
    window.show()
    app.exec()
    worker.join()
    if window.finished:
        return 0
    return 3


if __name__ == "__main__":
    sys.exit(main())

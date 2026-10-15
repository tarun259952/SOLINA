"""
Solina - Text to Speech (Windows, 64-bit)
=========================================
Converts text (typed, .txt, .pdf or .epub) into MP3 audio files using the
Windows SAPI5 voices installed on the PC.

Run the app:        python solina.py
Run diagnostics:    python solina.py --diag

Requirements (pip):
    ttkbootstrap pyttsx3 comtypes pywin32 pydub PyMuPDF ebooklib beautifulsoup4
External:
    ffmpeg (needed for MP3 export) - on PATH, or ffmpeg.exe next to this file.
"""

# =============================================================================
# 1. Environment guards (must run BEFORE the Windows-only imports below)
# =============================================================================
import platform
import sys

if sys.maxsize <= 2 ** 32:
    raise SystemExit(
        "Solina requires a 64-bit Python build.\n"
        f"Detected: {platform.architecture()[0]} on {platform.machine()}."
    )
if platform.system() != "Windows":
    raise SystemExit("Solina uses Windows SAPI5 voices and only runs on Windows.")

# =============================================================================
# 2. Imports (everything is imported once, here)
# =============================================================================
import os
import queue
import re
import shutil
import tempfile
import threading
import time
import traceback
from pathlib import Path

import tkinter as tk
from tkinter import filedialog, messagebox

import ttkbootstrap as tb
from ttkbootstrap.constants import BOTH, X, LEFT, RIGHT, W, END, HORIZONTAL

import pyttsx3                       # only used to list the installed voices
import fitz                          # PyMuPDF  (.pdf)
import ebooklib                      # .epub
from ebooklib import epub
from bs4 import BeautifulSoup, NavigableString
from pydub import AudioSegment       # WAV -> MP3 (needs ffmpeg)

import pythoncom                     # COM init for worker threads
import comtypes.client               # direct SAPI5 access (speak + export)

# =============================================================================
# 3. Paths + logging
# =============================================================================
# When packaged with PyInstaller, use the folder of the .exe, not the temp dir.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    try:
        APP_DIR = Path(__file__).resolve().parent     # running as solina.py
    except NameError:
        APP_DIR = Path.cwd()                          # running inside Jupyter

LOG_PATH = APP_DIR / "solina_log.txt"


def log_exc(context: str, exc: BaseException) -> str:
    """
    Append the full traceback of `exc` to solina_log.txt.
    Returns a one-line summary for the UI. Never raises.
    """
    tb_str = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    stamp = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"\n[{stamp}] {context}\n{tb_str}\n")
    except Exception:
        pass
    return f"{context}: {type(exc).__name__}: {exc}"


# =============================================================================
# 4. Text extraction (.txt / .pdf / .epub)
# =============================================================================
def _read_txt(path: str) -> str:
    """Read a text file, trying the common Windows encodings."""
    raw = Path(path).read_bytes()
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        return raw.decode("utf-16")
    for enc in ("utf-8-sig", "cp1252"):
        try:
            return raw.decode(enc)
        except UnicodeError:
            continue
    return raw.decode("latin-1")


def _read_pdf(path: str) -> str:
    """Extract text from every page of a PDF (pages separated by a blank line)."""
    with fitz.open(path) as doc:
        pages = [page.get_text("text") for page in doc]
    text = "\n\n".join(p.strip() for p in pages if p.strip())
    if not text:
        raise ValueError("No extractable text in this PDF (scanned PDFs need OCR).")
    return text


_BLOCK_2NL = ["p", "blockquote", "h1", "h2", "h3", "h4", "h5", "h6"]
_BLOCK_1NL = ["div", "li", "tr"]


def _read_epub(path: str) -> str:
    """
    Extract text from an EPUB in reading order (the spine), stripping HTML.
    Each chapter/section is separated by two blank lines, so the
    "two empty lines" split mode splits by chapter.
    """
    book = epub.read_epub(str(path), options={"ignore_ncx": True})

    items = []
    for entry in book.spine:
        item_id = entry[0] if isinstance(entry, tuple) else entry
        item = book.get_item_with_id(item_id)
        if item is not None and item.get_type() == ebooklib.ITEM_DOCUMENT:
            items.append(item)
    if not items:                                   # fallback: no usable spine
        items = list(book.get_items_of_type(ebooklib.ITEM_DOCUMENT))

    sections = []
    for item in items:
        soup = BeautifulSoup(item.get_content(), "html.parser")
        for tag in soup(["script", "style"]):
            tag.decompose()
        # Collapse source-code whitespace so only block tags create line breaks.
        for s in soup.find_all(string=True):
            if type(s) is NavigableString:
                s.replace_with(re.sub(r"\s+", " ", str(s)))
        for br in soup.find_all("br"):
            br.replace_with("\n")
        for tag in soup.find_all(_BLOCK_2NL):
            tag.append("\n\n")
        for tag in soup.find_all(_BLOCK_1NL):
            tag.append("\n")
        text = soup.get_text()
        text = re.sub(r"[ \t]*\n[ \t]*", "\n", text)
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        if text:
            sections.append(text)

    if not sections:
        raise ValueError("No readable text found in this EPUB.")
    return "\n\n\n".join(sections)


def extract_text(path: str) -> str:
    """Return the text of a .txt, .pdf or .epub file."""
    ext = Path(path).suffix.lower()
    if ext == ".txt":
        return _read_txt(path)
    if ext == ".pdf":
        return _read_pdf(path)
    if ext == ".epub":
        return _read_epub(path)
    raise ValueError(f"Unsupported file type: {ext or '(none)'}")


# =============================================================================
# 5. Text splitting
# =============================================================================
def _nonempty(chunks: list[str]) -> list[str]:
    """Strip chunks and drop the empty ones."""
    return [c.strip() for c in chunks if c and c.strip()]


def split_by_chars(text: str, size: int) -> list[str]:
    """Blocks of about `size` characters, cut at a sentence/line/word end."""
    if size < 1:
        raise ValueError("block size must be at least 1")
    chunks, i, n = [], 0, len(text)
    while i < n:
        end = min(i + size, n)
        if end < n:
            window = text[i:end]
            cut = max(window.rfind(". "), window.rfind("! "),
                      window.rfind("? "), window.rfind("\n"))
            if cut >= size // 2:
                end = i + cut + 1
            else:
                sp = window.rfind(" ")
                if sp >= size // 2:
                    end = i + sp + 1
        chunks.append(text[i:end])
        i = end
    return _nonempty(chunks)


def split_by_words(text: str, count: int) -> list[str]:
    """Blocks of `count` words (original spacing and line breaks are kept)."""
    if count < 1:
        raise ValueError("word count must be at least 1")
    words = list(re.finditer(r"\S+", text))
    chunks = []
    for k in range(0, len(words), count):
        start = words[k].start()
        end = words[min(k + count, len(words)) - 1].end()
        chunks.append(text[start:end])
    return _nonempty(chunks)


def split_by_keywords(text: str, keywords: list[str], strip: bool = True) -> list[str]:
    """
    Start a new block at every line that begins with one of the keywords
    (case-insensitive). If `strip`, the keyword itself is removed from the text.
    """
    keys = sorted({k.strip() for k in keywords if k.strip()}, key=len, reverse=True)
    if not keys:
        raise ValueError("enter at least one keyword")
    pattern = re.compile(
        r"^[ \t]*(?:" + "|".join(re.escape(k) for k in keys) + r")(?![A-Za-z0-9])",
        re.IGNORECASE | re.MULTILINE,
    )
    matches = list(pattern.finditer(text))
    if not matches:
        return _nonempty([text])
    chunks = [text[:matches[0].start()]]            # text before the first keyword
    for idx, m in enumerate(matches):
        end = matches[idx + 1].start() if idx + 1 < len(matches) else len(text)
        chunks.append(text[(m.end() if strip else m.start()):end])
    return _nonempty(chunks)


def split_by_blank_lines(text: str) -> list[str]:
    """Split wherever two empty lines follow each other."""
    text = text.replace("\r\n", "\n")
    return _nonempty(re.split(r"\n[ \t]*\n[ \t]*\n", text))


def _has_text(line: str) -> bool:
    """True if the line contains at least one letter or digit."""
    return bool(re.search(r"[^\W_]", line))


def _is_caps_heading(line: str) -> bool:
    """True for a short line whose letters are all capitals (chapter heading)."""
    s = line.strip()
    return bool(s) and len(s) <= 120 and any(c.isalpha() for c in s) and s == s.upper()


def split_by_capital_lines(text: str) -> list[str]:
    """Start a new block at each ALL-CAPS heading line (headings stay with their text)."""
    chunks, cur, body_seen = [], [], False
    for line in text.splitlines():
        if _is_caps_heading(line):
            if body_seen:                            # heading after real text -> new block
                chunks.append("\n".join(cur))
                cur, body_seen = [], False
        elif _has_text(line):
            body_seen = True
        cur.append(line)
    chunks.append("\n".join(cur))
    return _nonempty(chunks)


def clean_for_speech(chunk: str) -> str:
    """Drop decoration-only lines such as '=======' or '-------' before speaking."""
    lines = [ln for ln in chunk.splitlines() if not ln.strip() or _has_text(ln)]
    return "\n".join(lines).strip()


# ---- Output file naming ------------------------------------------------------
_BAD_FILENAME_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def safe_prefix(prefix: str) -> str:
    """Remove characters Windows does not allow in file names (spaces are kept)."""
    return _BAD_FILENAME_CHARS.sub("", prefix).lstrip()


def build_output_name(prefix: str, number: int, digits: int) -> str:
    """('tk ', 1, 2) -> 'tk 01.mp3'"""
    return f"{safe_prefix(prefix)}{number:0{digits}d}.mp3"


# =============================================================================
# 6. TTS engine (direct SAPI5 for BOTH preview and file export)
# =============================================================================
_CTRL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f]")


class TTSEngine:
    """
    Voice listing uses pyttsx3; speaking and WAV export use SAPI5 directly
    (SpVoice + SpFileStream), so pitch works and exports are reliable.
    The calling thread must have called pythoncom.CoInitialize().
    """

    def __init__(self):
        probe = pyttsx3.init(driverName="sapi5")
        self._voices = probe.getProperty("voices") or []

    def list_voices(self) -> list[dict]:
        """Installed SAPI5 voices as dicts: {id, name}."""
        return [{"id": v.id, "name": v.name} for v in self._voices]

    @staticmethod
    def _select_voice(voice, voice_id: str | None) -> None:
        """Select a voice by its token Id; fail loudly if it does not exist."""
        if not voice_id:
            return
        tokens = voice.GetVoices()
        for i in range(tokens.Count):
            tok = tokens.Item(i)
            if tok.Id == voice_id:
                voice.Voice = tok
                return
        raise RuntimeError(f"Voice not found: {voice_id}")

    @staticmethod
    def _payload(text: str, pitch: int):
        """Return (text_or_xml, speak_flags). Pitch needs SAPI XML (flag 8)."""
        text = _CTRL_CHARS.sub(" ", text)
        pitch = int(pitch)
        if pitch == 0:
            return text, 0
        sapi_pitch = max(-10, min(10, round(pitch / 5)))      # -50..50 -> -10..10
        safe = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return f'<pitch absmiddle="{sapi_pitch}"/>{safe}', 8   # 8 = SVSFIsXML

    def _speak(self, text, voice_id, rate, pitch, volume, wav_path=None) -> None:
        """Speak `text` (blocking). With wav_path, write a WAV file instead of playing."""
        voice = comtypes.client.CreateObject("SAPI.SpVoice")
        stream = None
        try:
            self._select_voice(voice, voice_id)
            voice.Rate = max(-10, min(10, round((int(rate) - 180) / 20)))   # WPM -> -10..10
            voice.Volume = int(max(0.0, min(1.0, float(volume))) * 100)      # 0..1 -> 0..100
            if wav_path is not None:
                stream = comtypes.client.CreateObject("SAPI.SpFileStream")
                stream.Open(str(wav_path), 3)                                # 3 = create for write
                voice.AudioOutputStream = stream
            payload, flags = self._payload(text, pitch)
            voice.Speak(payload, flags)                                      # synchronous
        finally:
            if stream is not None:
                try:
                    stream.Close()          # finalizes the WAV header on disk
                except Exception:
                    pass

    def preview(self, text, voice_id, rate, pitch, volume) -> None:
        """Play a short sample through the speakers (own COM apartment)."""
        pythoncom.CoInitialize()
        try:
            self._speak(text, voice_id, rate, pitch, volume)
        finally:
            pythoncom.CoUninitialize()

    def render_to_wav(self, text, wav_path, voice_id, rate, pitch, volume) -> None:
        """Render `text` to a WAV file. Caller must have called CoInitialize()."""
        self._speak(text, voice_id, rate, pitch, volume, wav_path=Path(wav_path))


# =============================================================================
# 7. WAV -> MP3 (pydub + ffmpeg)
# =============================================================================
def _find_ffmpeg() -> str | None:
    """Locate ffmpeg: PATH first, then next to the app / PyInstaller bundle."""
    found = shutil.which("ffmpeg")
    if found:
        return found
    folders = [APP_DIR]
    if getattr(sys, "_MEIPASS", None):
        folders.append(Path(sys._MEIPASS))
    for folder in folders:
        for name in ("ffmpeg.exe", "ffmpeg"):
            candidate = folder / name
            if candidate.is_file():
                return str(candidate)
    return None


def wav_to_mp3(wav_path: Path, mp3_path: Path) -> None:
    """Convert a WAV to MP3, verify the result, delete the WAV."""
    ffmpeg = _find_ffmpeg()
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg was not found.\n"
            "Install it with:  winget install Gyan.FFmpeg\n"
            "then restart the app, or put ffmpeg.exe next to solina.py."
        )
    AudioSegment.converter = ffmpeg
    probe = Path(ffmpeg).with_name("ffprobe.exe" if ffmpeg.lower().endswith(".exe") else "ffprobe")
    if probe.is_file():
        AudioSegment.ffprobe = str(probe)

    wav_path, mp3_path = Path(wav_path), Path(mp3_path)
    try:
        audio = AudioSegment.from_wav(str(wav_path))
    except Exception as e:
        raise RuntimeError(f"Could not read WAV {wav_path.name}: {e}") from e

    audio.export(str(mp3_path), format="mp3", bitrate="128k")

    if not mp3_path.exists() or mp3_path.stat().st_size == 0:
        raise RuntimeError(f"MP3 was not written or is empty: {mp3_path}")
    try:
        wav_path.unlink()
    except Exception:
        pass


# =============================================================================
# 8. Diagnostics   (python solina.py --diag)
# =============================================================================
def run_diagnostics() -> None:
    """PASS/FAIL checks for every step of the pipeline. Not run automatically."""
    results = []

    def record(name: str, ok: bool, detail: str = "") -> None:
        results.append(ok)
        print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))

    print("=" * 60 + "\n Solina diagnostics\n" + "=" * 60)
    record("64-bit Python", sys.maxsize > 2 ** 32,
           f"{platform.architecture()[0]}, Python {sys.version.split()[0]}")
    ff = _find_ffmpeg()
    record("ffmpeg located", ff is not None, ff or "install: winget install Gyan.FFmpeg")

    out = Path.home() / "Solina_Diag"
    try:
        out.mkdir(parents=True, exist_ok=True)
        probe = out / ".write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
        record("Output folder writable", True, str(out))
    except Exception as e:
        record("Output folder writable", False, str(e))

    try:
        tts = TTSEngine()
        voices = tts.list_voices()
        record("SAPI voices enumerated", len(voices) > 0, f"{len(voices)} voice(s)")
    except Exception as e:
        log_exc("diagnostic: enumerate voices", e)
        record("SAPI voices enumerated", False, str(e))
        return
    if not voices:
        return

    # Test the first voice (pitch 0 and pitch 20) and a Native voice.
    native = (next((v for v in voices if "Ava Native" in v["name"]), None)
              or next((v for v in voices if "Native" in v["name"]), None))
    tests = [(voices[0], 0), (voices[0], 20)]
    if native and native["id"] != voices[0]["id"]:
        tests.append((native, 0))

    pythoncom.CoInitialize()
    try:
        for n, (v, pitch) in enumerate(tests, 1):
            label = f"{v['name']} (pitch {pitch})"
            wav, mp3 = out / f"diag_{n}.wav", out / f"diag_{n}.mp3"
            t0 = time.time()
            try:
                tts.render_to_wav("Diagnostic test sentence.", wav, v["id"], 180, pitch, 1.0)
                wav_ok = wav.exists() and wav.stat().st_size > 0
                record(f"WAV export - {label}", wav_ok,
                       f"{wav.stat().st_size if wav.exists() else 0} bytes, {time.time() - t0:.1f}s")
                if wav_ok:
                    wav_to_mp3(wav, mp3)
                    record(f"MP3 conversion - {label}",
                           mp3.exists() and mp3.stat().st_size > 0, f"{mp3.stat().st_size} bytes")
            except Exception as e:
                log_exc(f"diagnostic: {label}", e)
                record(f"Export - {label}", False, str(e))
    finally:
        pythoncom.CoUninitialize()

    print("=" * 60)
    print(f" Summary: {sum(results)}/{len(results)} checks passed")
    print(f" Log file: {LOG_PATH}")
    print("=" * 60)


# =============================================================================
# 9. GUI
# =============================================================================
ACCENT = "#4ea1ff"      # single accent colour
BG_MAIN = "#2b2b2b"     # window background
BG_DARK = "#1e1e1e"     # text-area background
FG_TEXT = "#e6e6e6"     # light-grey text


class SolinaApp(tb.Window):
    """Solina main window."""

    # --------------------------------------------------------------- lifecycle
    def __init__(self):
        super().__init__(themename="darkly")
        self.title("Solina - Text to Speech")
        self.geometry("1180x800")
        self.minsize(960, 640)
        self.configure(background=BG_MAIN)

        self.tts = TTSEngine()
        self._msg_q: queue.Queue = queue.Queue()     # worker threads -> UI
        self._busy = False                           # export running
        self._previewing = False                     # preview running

        self.voice_choice = tk.StringVar()
        self.rate_var = tk.IntVar(value=180)
        self.pitch_var = tk.IntVar(value=0)
        self.volume_var = tk.DoubleVar(value=1.0)

        self.split_mode = tk.StringVar(value="chars")
        self.char_count = tk.StringVar(value="3000")
        self.word_count = tk.StringVar(value="500")
        self.keywords = tk.StringVar(value="")
        self.strip_kw = tk.BooleanVar(value=True)

        self.name_prefix = tk.StringVar(value="Solina_Part_")   # output file name prefix
        self.name_start = tk.StringVar(value="1")               # first file number
        self.name_digits = tk.StringVar(value="3")              # zero-padding width

        self.output_dir = tk.StringVar(value=str(Path.home() / "Solina_Output"))
        self.status_var = tk.StringVar(value="Ready")
        self.file_var = tk.StringVar(value="(no file loaded)")

        self._voice_map: dict[str, str] = {}         # display label -> SAPI voice id

        self._build_ui()
        self._populate_voices()
        self.protocol("WM_DELETE_WINDOW", self.destroy)
        self._set_window_icon()
        self.after(100, self._poll_queue)

    def _set_window_icon(self):
        """Use solina.ico for the window/taskbar icon (keeps the default if missing)."""
        for base in (Path(getattr(sys, "_MEIPASS", APP_DIR)), APP_DIR):
            ico = base / "solina.ico"
            if ico.is_file():
                try:
                    self.iconbitmap(str(ico))
                except Exception:
                    pass
                return

    # ------------------------------------------------------------------ build
    def _build_ui(self):
        """Create every widget once."""
        style = self.style
        style.configure("Solina.TFrame", background=BG_MAIN)
        style.configure("Solina.TLabel", background=BG_MAIN, foreground=FG_TEXT)
        style.configure("Solina.TLabelframe", background=BG_MAIN,
                        foreground=FG_TEXT, bordercolor="#3c3c3c")
        style.configure("Solina.TLabelframe.Label", background=BG_MAIN, foreground=FG_TEXT)
        style.configure("Solina.TRadiobutton", background=BG_MAIN, foreground=FG_TEXT)
        style.configure("Solina.TCheckbutton", background=BG_MAIN, foreground=FG_TEXT)

        root = tb.Frame(self, padding=14, style="Solina.TFrame")
        root.pack(fill=BOTH, expand=True)
        root.columnconfigure(0, weight=1)
        root.columnconfigure(1, weight=0)
        root.rowconfigure(1, weight=1)

        # ---- Row 0: toolbar ------------------------------------------------
        bar = tb.Frame(root, style="Solina.TFrame")
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
        tb.Button(bar, text="Import file...", bootstyle="secondary",
                  command=self._on_import).pack(side=LEFT)
        tb.Label(bar, textvariable=self.file_var, style="Solina.TLabel"
                 ).pack(side=LEFT, padx=(10, 0))
        tb.Button(bar, text="Browse...", bootstyle="secondary",
                  command=self._on_browse_output).pack(side=RIGHT)
        tb.Label(bar, textvariable=self.output_dir, style="Solina.TLabel"
                 ).pack(side=RIGHT, padx=(0, 10))
        tb.Label(bar, text="Output folder:", style="Solina.TLabel").pack(side=RIGHT)

        # ---- Row 1 left: text editor ---------------------------------------
        left = tb.Frame(root, style="Solina.TFrame")
        left.grid(row=1, column=0, sticky="nsew", padx=(0, 14))
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        tb.Label(left, text="Text content", style="Solina.TLabel",
                 font=("Segoe UI", 12, "bold")).grid(row=0, column=0, sticky=W)

        text_wrap = tk.Frame(left, bg=BG_DARK)
        text_wrap.grid(row=1, column=0, sticky="nsew", pady=(6, 0))
        text_wrap.rowconfigure(0, weight=1)
        text_wrap.columnconfigure(0, weight=1)
        self.text = tk.Text(
            text_wrap, wrap="word", undo=True,
            bg=BG_DARK, fg=FG_TEXT, insertbackground=FG_TEXT,
            selectbackground=ACCENT, selectforeground="#000000",
            relief="flat", padx=12, pady=12, font=("Consolas", 11),
        )
        self.text.grid(row=0, column=0, sticky="nsew")
        sb = tb.Scrollbar(text_wrap, orient="vertical", command=self.text.yview)
        sb.grid(row=0, column=1, sticky="ns")
        self.text.configure(yscrollcommand=sb.set)

        # ---- Row 1 right: controls -----------------------------------------
        right = tb.Frame(root, style="Solina.TFrame")
        right.grid(row=1, column=1, sticky="ns")

        audio_lf = tb.Labelframe(right, text="Voice & audio", padding=12,
                                 style="Solina.TLabelframe")
        audio_lf.pack(fill=X, pady=(0, 10))
        tb.Label(audio_lf, text="Voice", style="Solina.TLabel").pack(anchor=W)
        self.voice_combo = tb.Combobox(audio_lf, textvariable=self.voice_choice,
                                       state="readonly", width=38)
        self.voice_combo.pack(fill=X, pady=(2, 10))
        self._slider(audio_lf, "Rate", self.rate_var, 50, 400)
        self._slider(audio_lf, "Pitch", self.pitch_var, -50, 50)
        self._slider(audio_lf, "Volume", self.volume_var, 0.0, 1.0)
        self.test_btn = tb.Button(audio_lf, text="Test voice", bootstyle="info",
                                  command=self._on_test_voice)
        self.test_btn.pack(fill=X, pady=(10, 0))

        split_lf = tb.Labelframe(right, text="Text splitting", padding=12,
                                 style="Solina.TLabelframe")
        split_lf.pack(fill=X)

        def radio(value, label):
            tb.Radiobutton(split_lf, text=label, value=value, variable=self.split_mode,
                           style="Solina.TRadiobutton").pack(anchor=W)

        radio("chars", "By block size - characters")
        row = tb.Frame(split_lf, style="Solina.TFrame")
        row.pack(fill=X, padx=(22, 0))
        tb.Entry(row, textvariable=self.char_count, width=8).pack(side=LEFT)
        tb.Label(row, text="characters", style="Solina.TLabel").pack(side=LEFT, padx=6)

        radio("words", "By block size - words")
        row = tb.Frame(split_lf, style="Solina.TFrame")
        row.pack(fill=X, padx=(22, 0))
        tb.Entry(row, textvariable=self.word_count, width=8).pack(side=LEFT)
        tb.Label(row, text="words", style="Solina.TLabel").pack(side=LEFT, padx=6)

        radio("keywords", "By keyword list (comma separated)")
        row = tb.Frame(split_lf, style="Solina.TFrame")
        row.pack(fill=X, padx=(22, 0))
        tb.Entry(row, textvariable=self.keywords, width=26).pack(side=LEFT)
        tb.Checkbutton(split_lf, text="Strip keyword from output", variable=self.strip_kw,
                       style="Solina.TCheckbutton").pack(anchor=W, padx=(22, 0))

        radio("blank", "By two consecutive empty lines")
        radio("caps", "By lines that are ALL CAPS (headings)")

        # ---- Output file name ----------------------------------------------
        name_lf = tb.Labelframe(right, text="Output file name", padding=12,
                                style="Solina.TLabelframe")
        name_lf.pack(fill=X, pady=(10, 0))
        row = tb.Frame(name_lf, style="Solina.TFrame")
        row.pack(fill=X)
        tb.Label(row, text="Name", width=7, style="Solina.TLabel").pack(side=LEFT)
        tb.Entry(row, textvariable=self.name_prefix, width=22).pack(side=LEFT, fill=X, expand=True)
        row = tb.Frame(name_lf, style="Solina.TFrame")
        row.pack(fill=X, pady=(6, 0))
        tb.Label(row, text="Start at", style="Solina.TLabel").pack(side=LEFT)
        tb.Entry(row, textvariable=self.name_start, width=5).pack(side=LEFT, padx=(6, 14))
        tb.Label(row, text="Digits", style="Solina.TLabel").pack(side=LEFT)
        tb.Entry(row, textvariable=self.name_digits, width=3).pack(side=LEFT, padx=6)
        self.name_example = tk.StringVar()
        tb.Label(name_lf, textvariable=self.name_example, style="Solina.TLabel"
                 ).pack(anchor=W, pady=(8, 0))
        for var in (self.name_prefix, self.name_start, self.name_digits):
            var.trace_add("write", lambda *_: self._update_name_example())
        self._update_name_example()

        self.convert_btn = tb.Button(right, text="Convert to MP3", bootstyle="success",
                                     command=self._on_convert)
        self.convert_btn.pack(fill=X, pady=(12, 0))

        # ---- Row 2: status + progress --------------------------------------
        bottom = tb.Frame(root, style="Solina.TFrame")
        bottom.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(12, 0))
        bottom.columnconfigure(0, weight=1)
        self.progress = tb.Progressbar(bottom, mode="determinate", bootstyle="info-striped")
        self.progress.grid(row=0, column=0, sticky="ew")
        tb.Label(bottom, textvariable=self.status_var, style="Solina.TLabel"
                 ).grid(row=1, column=0, sticky=W, pady=(6, 0))

    def _slider(self, parent, label, var, lo, hi):
        """Labelled horizontal slider."""
        row = tb.Frame(parent, style="Solina.TFrame")
        row.pack(fill=X, pady=(2, 0))
        tb.Label(row, text=label, width=7, style="Solina.TLabel").pack(side=LEFT)
        tb.Scale(row, from_=lo, to=hi, variable=var, orient=HORIZONTAL
                 ).pack(side=LEFT, fill=X, expand=True)

    def _naming(self):
        """Return (prefix, start, digits) from the UI; raises ValueError if invalid."""
        start, digits = int(self.name_start.get()), int(self.name_digits.get())
        if start < 0:
            raise ValueError("'Start at' must be 0 or more")
        if not 1 <= digits <= 6:
            raise ValueError("'Digits' must be between 1 and 6")
        return self.name_prefix.get(), start, digits

    def _update_name_example(self):
        """Show a live example of the file names that will be created."""
        try:
            prefix, start, digits = self._naming()
            self.name_example.set(
                "Example: " + build_output_name(prefix, start, digits)
                + ", " + build_output_name(prefix, start + 1, digits))
        except ValueError:
            self.name_example.set("Example: (check Start at / Digits)")

    # ------------------------------------------------------------------ voices
    def _populate_voices(self):
        """Fill the dropdown. Duplicate voice names get a #2, #3 suffix."""
        self._voice_map.clear()
        labels = []
        for v in self.tts.list_voices():
            label, n = v["name"], 2
            while label in self._voice_map:
                label = f'{v["name"]} #{n}'
                n += 1
            self._voice_map[label] = v["id"]
            labels.append(label)
        self.voice_combo["values"] = labels
        if labels:
            self.voice_combo.current(0)

    # --------------------------------------------------------------- callbacks
    def _on_import(self):
        """Load .txt/.pdf/.epub into the text box."""
        path = filedialog.askopenfilename(
            title="Choose a source file",
            filetypes=[("Supported files", "*.txt *.pdf *.epub"),
                       ("Text", "*.txt"), ("PDF", "*.pdf"), ("EPUB", "*.epub")],
        )
        if not path:
            return
        self.status_var.set("Reading file...")
        self.update_idletasks()
        try:
            content = extract_text(path)
        except Exception as e:
            log_exc("import", e)
            self.status_var.set("Import failed.")
            messagebox.showerror("Solina", f"Failed to read file:\n{e}")
            return
        self.text.delete("1.0", END)
        self.text.insert("1.0", content)
        self.file_var.set(Path(path).name)
        self.status_var.set(f"Loaded {Path(path).name}")

    def _on_browse_output(self):
        """Pick the output folder for the MP3 files."""
        folder = filedialog.askdirectory(title="Choose output folder")
        if folder:
            self.output_dir.set(folder)

    def _on_test_voice(self):
        """Speak a sample with the current settings (no file is written)."""
        if self._busy or self._previewing:
            return
        voice_id = self._voice_map.get(self.voice_choice.get())
        rate, pitch, vol = self.rate_var.get(), self.pitch_var.get(), self.volume_var.get()
        sample = "Hello, this is Solina. This is how your audiobook will sound."
        self._previewing = True
        self.test_btn.configure(state="disabled")
        self.status_var.set("Previewing voice...")

        def worker():
            try:
                self.tts.preview(sample, voice_id, rate, pitch, vol)
                self._msg_q.put(("preview_done", "Preview finished."))
            except Exception as e:
                self._msg_q.put(("preview_done", "Preview failed: " + log_exc("preview", e)))

        threading.Thread(target=worker, daemon=True).start()

    def _make_chunks(self, text: str) -> list[str]:
        """Apply the selected splitting mode (raises ValueError on bad input)."""
        mode = self.split_mode.get()
        if mode == "chars":
            return split_by_chars(text, int(self.char_count.get()))
        if mode == "words":
            return split_by_words(text, int(self.word_count.get()))
        if mode == "keywords":
            return split_by_keywords(text, self.keywords.get().split(","),
                                     strip=self.strip_kw.get())
        if mode == "blank":
            return split_by_blank_lines(text)
        if mode == "caps":
            return split_by_capital_lines(text)
        return [text]

    # ---------------------------------------------------------------- convert
    def _on_convert(self):
        """Validate, confirm the file count, then start the export thread."""
        if self._busy or self._previewing:
            return

        text = self.text.get("1.0", END).strip()
        if not text:
            messagebox.showwarning("Solina", "There is no text to convert.")
            return

        voice_label = self.voice_choice.get()
        voice_id = self._voice_map.get(voice_label)
        if not voice_id:
            messagebox.showwarning("Solina", "Please choose a voice from the dropdown.")
            return

        if _find_ffmpeg() is None:
            messagebox.showerror(
                "Solina - ffmpeg missing",
                "ffmpeg was not found. MP3 export needs it.\n\n"
                "Install with:  winget install Gyan.FFmpeg\n"
                "then restart Solina, or place ffmpeg.exe next to solina.py.")
            return

        try:
            chunks = self._make_chunks(text)
        except ValueError as e:
            messagebox.showerror("Solina", f"Invalid split setting: {e}")
            return
        chunks = [c for c in (clean_for_speech(c) for c in chunks) if c]
        if not chunks:
            messagebox.showwarning("Solina", "The split settings produced 0 blocks.")
            return

        try:
            prefix, start, digits = self._naming()
        except ValueError as e:
            messagebox.showerror("Solina", f"Invalid file name setting: {e}")
            return

        if not messagebox.askyesno(
                "Solina", f"Approximate number of files: {len(chunks)}\n\nProceed with export?"):
            return

        out_dir = Path(self.output_dir.get()).expanduser()
        try:
            os.makedirs(out_dir, exist_ok=True)
        except Exception as e:
            log_exc("create output folder", e)
            messagebox.showerror("Solina", f"Could not create output folder:\n{e}")
            return

        # Warn before overwriting files that already exist with the same names.
        existing = [build_output_name(prefix, start + k, digits) for k in range(len(chunks))
                    if (out_dir / build_output_name(prefix, start + k, digits)).exists()]
        if existing and not messagebox.askyesno(
                "Solina", f"{len(existing)} file(s) already exist (e.g. {existing[0]}).\n\n"
                          "Overwrite them?"):
            return

        # Snapshot Tk values: worker threads must not touch Tk variables.
        settings = {
            "voice_id": voice_id, "voice_label": voice_label,
            "rate": self.rate_var.get(), "pitch": self.pitch_var.get(),
            "volume": self.volume_var.get(),
            "prefix": prefix, "start": start, "digits": digits,
        }

        self._busy = True
        self.convert_btn.configure(state="disabled")
        self.test_btn.configure(state="disabled")
        self.progress.configure(maximum=len(chunks), value=0)
        self.status_var.set(f"Starting - 0 / {len(chunks)}")
        threading.Thread(target=self._convert_worker,
                         args=(chunks, out_dir, settings), daemon=True).start()

    # ----------------------------------------------------------------- worker
    def _convert_worker(self, chunks, out_dir: Path, settings: dict):
        """Background thread: text block -> WAV (SAPI5) -> MP3, reporting via the queue."""
        total = len(chunks)
        ok = 0
        errors: list[str] = []
        label = settings["voice_label"]
        tmp_dir = Path(tempfile.mkdtemp(prefix="solina_"))
        com_ready = False
        try:
            pythoncom.CoInitialize()          # COM apartment for THIS thread
            com_ready = True
            for i, chunk in enumerate(chunks, start=1):
                wav_path = tmp_dir / f"part_{i:03d}.wav"
                mp3_path = out_dir / build_output_name(
                    settings["prefix"], settings["start"] + i - 1, settings["digits"])
                self._msg_q.put(("status", f"Converting part {i}/{total}..."))
                try:
                    self.tts.render_to_wav(chunk, wav_path, settings["voice_id"],
                                           settings["rate"], settings["pitch"],
                                           settings["volume"])
                    if not wav_path.exists() or wav_path.stat().st_size == 0:
                        raise RuntimeError(f"SAPI produced no audio for part {i:03d}")
                    wav_to_mp3(wav_path, mp3_path)
                    ok += 1
                except Exception as e:
                    errors.append(log_exc(f"Part {i:03d}", e)[:220])
                finally:
                    try:
                        wav_path.unlink(missing_ok=True)
                    except Exception:
                        pass
                self._msg_q.put(("progress", i, total))
        except Exception as e:
            errors.append(log_exc("export worker", e)[:220])
        finally:
            shutil.rmtree(tmp_dir, ignore_errors=True)
            if com_ready:
                pythoncom.CoUninitialize()
            if errors and any(k in label.lower() for k in ("online", "natural")):
                errors.append(f"Tip: '{label}' looks like an online voice; SAPI often cannot "
                              "export those. Try a Native or Desktop voice.")
            self._msg_q.put(("done", ok, total, str(out_dir), errors))

    # ---------------------------------------------------------- queue poller
    def _poll_queue(self):
        """Runs on the UI thread every 100 ms and applies worker messages."""
        try:
            while True:
                msg = self._msg_q.get_nowait()
                kind = msg[0]
                if kind == "status":
                    self.status_var.set(msg[1])
                elif kind == "progress":
                    self.progress.configure(value=msg[1])
                    self.status_var.set(f"Converted {msg[1]}/{msg[2]}")
                elif kind == "preview_done":
                    self._previewing = False
                    if not self._busy:
                        self.test_btn.configure(state="normal")
                    self.status_var.set(msg[1])
                elif kind == "done":
                    _, ok, total, folder, errors = msg
                    self._busy = False
                    self.convert_btn.configure(state="normal")
                    self.test_btn.configure(state="normal")
                    self.progress.configure(value=total)
                    self.status_var.set(f"Done - {ok}/{total} files written.")
                    self._show_completion(ok, total, folder, errors)
        except queue.Empty:
            pass
        self.after(100, self._poll_queue)

    def _show_completion(self, ok: int, total: int, folder: str, errors: list[str]):
        """Completion popup with an 'Open folder' button."""
        win = tk.Toplevel(self)
        win.title("Solina - Export complete")
        win.configure(bg=BG_MAIN)
        win.geometry("560x420")
        win.transient(self)
        win.grab_set()

        tk.Label(win, text=f"Exported {ok} of {total} MP3 file(s).", bg=BG_MAIN, fg=FG_TEXT,
                 font=("Segoe UI", 12, "bold")).pack(anchor="w", padx=16, pady=(16, 4))
        tk.Label(win, text=f"Folder:\n{folder}", bg=BG_MAIN, fg=FG_TEXT, justify="left",
                 wraplength=520, font=("Segoe UI", 10)).pack(anchor="w", padx=16)

        if errors:
            tk.Label(win, text="Some parts failed (details in solina_log.txt):",
                     bg=BG_MAIN, fg="#ff8080", font=("Segoe UI", 10, "bold")
                     ).pack(anchor="w", padx=16, pady=(12, 2))
            shown = "\n".join(errors[:6])
            if len(errors) > 6:
                shown += f"\n...and {len(errors) - 6} more."
            tk.Label(win, text=shown, bg=BG_MAIN, fg=FG_TEXT, justify="left",
                     wraplength=520, font=("Consolas", 9)).pack(anchor="w", padx=16)

        btns = tk.Frame(win, bg=BG_MAIN)
        btns.pack(side="bottom", fill="x", padx=16, pady=12)

        def open_folder():
            try:
                os.startfile(folder)
            except Exception as e:
                messagebox.showerror("Solina", f"Could not open folder:\n{e}")

        tb.Button(btns, text="Open folder", bootstyle="info", command=open_folder).pack(side="left")
        tb.Button(btns, text="Close", bootstyle="secondary", command=win.destroy).pack(side="right")


# =============================================================================
# 10. Entry point
# =============================================================================
def main():
    if "--diag" in sys.argv:
        run_diagnostics()
        return
    SolinaApp().mainloop()


if __name__ == "__main__":
    main()

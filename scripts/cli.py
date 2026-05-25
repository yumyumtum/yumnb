"""
yumnb — Yum NoteBook main CLI.

Subcommands:
  ingest <INPUT>           ingest URL / YouTube / image / text into a note folder
  summarize <FOLDER>       AI summary → summary.md (requires ai.provider != none)
  talkshow <FOLDER>        AI talk-show script → talkshow.txt (requires ai)
  deckplan <FOLDER>        AI deck plan → deck.json (requires ai)
  tts <SCRIPT> --output X  Render dual-voice MP3 from a [Speaker]-tagged script
  ppt <JSON> --output X    Render PPTX from a slide-plan JSON
  publish <FOLDER>         Write links.json, optionally notify webhook
  auto <INPUT>             Run end-to-end (ingest → summarize → talkshow → tts → deckplan → ppt → publish)
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict
from urllib.parse import urlparse

# Make sibling scripts importable when invoked as `python scripts/yumnb.py`
_HERE = Path(__file__).resolve().parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from make_ppt import build as build_pptx  # type: ignore
from tts import build_dual_audio  # type: ignore
import notify  # type: ignore
import ai_provider  # type: ignore
import upload as upload_mod  # type: ignore


# ── Config loading ────────────────────────────────────────────────────────

# Built-in default voice pairs for the most common languages. Users can
# override or add more languages via tts.language_voices in config.yaml.
_DEFAULT_LANGUAGE_VOICES = {
    "en": {
        "HostA": {"voice": "en-US-AndrewNeural", "rate": "+0%"},
        "HostB": {"voice": "en-US-AvaNeural",    "rate": "+0%"},
    },
    "zh": {
        "云飞": {"voice": "zh-CN-YunyangNeural",  "rate": "+10%"},
        "小晓": {"voice": "zh-CN-XiaoxiaoNeural", "rate": "+10%"},
    },
    "ja": {
        "HostA": {"voice": "ja-JP-KeitaNeural",  "rate": "+0%"},
        "HostB": {"voice": "ja-JP-NanamiNeural", "rate": "+0%"},
    },
    "es": {
        "HostA": {"voice": "es-ES-AlvaroNeural", "rate": "+0%"},
        "HostB": {"voice": "es-ES-ElviraNeural", "rate": "+0%"},
    },
    "fr": {
        "HostA": {"voice": "fr-FR-HenriNeural",   "rate": "+0%"},
        "HostB": {"voice": "fr-FR-DeniseNeural",  "rate": "+0%"},
    },
    "de": {
        "HostA": {"voice": "de-DE-ConradNeural",  "rate": "+0%"},
        "HostB": {"voice": "de-DE-KatjaNeural",   "rate": "+0%"},
    },
}

# Human-readable language names used in AI prompts.
_LANGUAGE_NAMES = {
    "en": "English", "zh": "Simplified Chinese (简体中文)", "ja": "Japanese (日本語)",
    "es": "Spanish (Español)", "fr": "French (Français)", "de": "German (Deutsch)",
    "ko": "Korean (한국어)", "pt": "Portuguese", "it": "Italian", "ru": "Russian",
}

_DEFAULT_CFG = {
    "output_dir": "./notes",
    "language": "en",  # default output language (en|zh|ja|es|fr|de|...)
    "ai": {"provider": "none"},
    "tts": {
        "enabled": True,
        "voices": {},                                  # explicit override (highest priority)
        "language_voices": _DEFAULT_LANGUAGE_VOICES,   # per-language defaults
        "jingle": True,
        "custom_jingle_mp3": "",
    },
    "upload": {
        "provider": "none",   # none | rclone
        "rclone": {
            "remote": "",            # e.g. "onedrive:yumnb" or "gdrive:yumnb"
            "rclone_bin": "rclone",
            "share": True,
            "per_note_subfolder": True,
            "extra_args": [],
        },
        "files": ["talkshow.mp3", "deck.pptx", "summary.md"],
    },
    "notify": {"webhook_url": None, "style": "generic"},
}


def _resolve_language(cfg: dict, override: str = "") -> str:
    lang = (override or os.environ.get("YUMNB_LANGUAGE") or cfg.get("language") or "en")
    return str(lang).strip().lower() or "en"


def _resolve_voices(cfg: dict, language: str) -> dict:
    """Pick the active TTS voice map.

    Priority: tts.voices (explicit) → tts.language_voices[language] →
    tts.language_voices['en'] → built-in English pair.
    """
    tts = cfg.get("tts") or {}
    if tts.get("voices"):
        return tts["voices"]
    lv = tts.get("language_voices") or {}
    if language in lv and lv[language]:
        return lv[language]
    if "en" in lv and lv["en"]:
        return lv["en"]
    return _DEFAULT_LANGUAGE_VOICES["en"]


def _deep_merge(base: dict, overlay: dict) -> dict:
    out = dict(base)
    for k, v in (overlay or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_config(explicit_path: str = "") -> Dict[str, Any]:
    """Locate and load config.yaml.

    Search order: explicit --config arg, env YUMNB_CONFIG, ./config.yaml,
    ./config.local.yaml, <skill_dir>/config.yaml.
    """
    candidates = []
    if explicit_path:
        candidates.append(Path(explicit_path))
    if os.environ.get("YUMNB_CONFIG"):
        candidates.append(Path(os.environ["YUMNB_CONFIG"]))
    candidates += [
        Path.cwd() / "config.local.yaml",
        Path.cwd() / "config.yaml",
        _HERE.parent / "config.yaml",
    ]

    raw: Dict[str, Any] = {}
    for p in candidates:
        if p.is_file():
            try:
                import yaml  # type: ignore
            except ImportError:
                print(f"WARN: PyYAML not installed, ignoring {p}", file=sys.stderr)
                break
            raw = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
            print(f"yumnb: loaded config from {p}")
            break

    cfg = _deep_merge(_DEFAULT_CFG, raw)
    # Env override for output_dir
    if os.environ.get("YUMNB_OUTPUT_DIR"):
        cfg["output_dir"] = os.environ["YUMNB_OUTPUT_DIR"]
    return cfg


# ── Utilities ─────────────────────────────────────────────────────────────

def slugify(s: str, maxlen: int = 30) -> str:
    s = re.sub(r"[\\/:*?\"<>|\r\n\t]+", "", s or "").strip()
    s = re.sub(r"\s+", "-", s)
    return (s[:maxlen] or "note").strip("-")


def detect_kind(inp: str) -> str:
    low = inp.lower()
    if "youtube.com" in low or "youtu.be" in low:
        return "youtube"
    if low.startswith(("http://", "https://")):
        return "url"
    if os.path.isfile(inp):
        ext = os.path.splitext(inp)[1].lower()
        if ext in (".png", ".jpg", ".jpeg", ".gif", ".bmp", ".webp", ".avif"):
            return "image"
        return "file"
    return "text"


def make_folder(output_dir: Path, kind: str, title_hint: str) -> Path:
    date = datetime.now().strftime("%Y%m%d-%H%M")
    slug = slugify(title_hint or kind)
    folder = output_dir / f"{date}-{slug}"
    (folder / "source").mkdir(parents=True, exist_ok=True)
    return folder


# ── Ingest backends ───────────────────────────────────────────────────────

def _parse_vtt(vtt_text: str) -> str:
    """Strip VTT timing/tags, dedupe consecutive identical lines."""
    out, last = [], ""
    for raw in vtt_text.splitlines():
        ln = raw.strip()
        if not ln or ln.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")) or "-->" in ln:
            continue
        ln = re.sub(r"<[^>]+>", "", ln).strip()
        if not ln or ln == last:
            continue
        out.append(ln)
        last = ln
    return "\n".join(out)


def ingest_youtube(url: str, src_dir: Path) -> dict:
    """yt-dlp: pull metadata + thumbnail + subtitles (manual first, then auto)."""
    try:
        import yt_dlp  # type: ignore
    except ImportError as e:
        raise SystemExit(f"yt-dlp not installed: {e}")

    info_path = src_dir / "youtube_info.json"

    opts_meta = {
        "skip_download": True, "quiet": True, "no_warnings": True,
        "writethumbnail": True, "writeinfojson": False,
        "outtmpl": str(src_dir / "thumb.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts_meta) as ydl:
        info = ydl.extract_info(url, download=True)

    meta = {k: info.get(k) for k in
            ("title", "uploader", "duration", "description", "view_count",
             "upload_date", "webpage_url", "id")}
    info_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    sub_base = src_dir / "sub"
    transcript_text, sub_source, sub_lang = "", "", ""
    LANG_ORDER = ["zh-Hans", "zh-CN", "zh", "en", "en-US"]
    for attempt in ("manual", "auto"):
        for lang in LANG_ORDER:
            opts_sub = {
                "skip_download": True, "quiet": True, "no_warnings": True,
                "writesubtitles": attempt == "manual",
                "writeautomaticsub": attempt == "auto",
                "subtitleslangs": [lang], "subtitlesformat": "vtt",
                "outtmpl": str(sub_base) + ".%(ext)s",
                "ignoreerrors": True,
            }
            try:
                with yt_dlp.YoutubeDL(opts_sub) as ydl:
                    ydl.extract_info(url, download=True)
            except Exception as e:
                print(f"[subs:{attempt}:{lang}] yt-dlp error: {e}")
                continue
            vtts = list(src_dir.glob(f"sub.{lang}.vtt"))
            if not vtts:
                continue
            try:
                txt = _parse_vtt(vtts[0].read_text(encoding="utf-8", errors="replace"))
                if txt.strip():
                    transcript_text, sub_source, sub_lang = txt, attempt, lang
                    break
            except Exception as e:
                print(f"[subs:{attempt}:{lang}] parse error: {e}")
        if transcript_text:
            break

    if not transcript_text.strip():
        transcript_text = (
            f"[no subtitles available — manual and auto both empty]\n\n"
            f"Description:\n{meta.get('description', '')}"
        )
        sub_source = "none"

    (src_dir / "transcript.txt").write_text(transcript_text, encoding="utf-8")
    meta["transcript_source"] = sub_source
    meta["transcript_lang"] = sub_lang
    info_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    (src_dir / "raw.txt").write_text(
        f"# {meta.get('title')}\n\n"
        f"Uploader: {meta.get('uploader')}\nDuration: {meta.get('duration')}s\n"
        f"URL: {meta.get('webpage_url')}\n"
        f"Transcript source: {sub_source} ({sub_lang})\n\n"
        f"## Transcript\n\n{transcript_text}\n\n"
        f"## Description\n\n{meta.get('description', '')}",
        encoding="utf-8",
    )
    return {"kind": "youtube", "title": meta.get("title") or "youtube", "meta": meta}


def ingest_url(url: str, src_dir: Path, fetcher: str = "") -> dict:
    """Fetch a web page. Falls back to an external fetcher command if given."""
    import requests

    html, title = "", ""
    try:
        r = requests.get(url, timeout=20, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        if r.status_code == 200:
            html = r.text
    except Exception as e:
        html = f"<!-- fetch failed: {e} -->"

    if (not html or "<title>" not in html.lower()) and fetcher:
        try:
            out = subprocess.run(fetcher.split() + [url], capture_output=True,
                                 text=True, encoding="utf-8", timeout=60)
            if out.returncode == 0 and out.stdout:
                html = out.stdout
        except Exception as e:
            print(f"fetcher fallback failed: {e}")

    (src_dir / "page.html").write_text(html, encoding="utf-8", errors="replace")

    # Prefer BeautifulSoup if installed; fall back to stdlib HTMLParser.
    text = ""
    try:
        from bs4 import BeautifulSoup  # type: ignore

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "noscript", "svg"]):
            tag.decompose()
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")
        text = re.sub(r"\n{3,}", "\n\n", soup.get_text("\n")).strip()
    except ImportError:
        try:
            from html.parser import HTMLParser

            class _T(HTMLParser):
                def __init__(self):
                    super().__init__()
                    self.parts = []
                    self.skip = 0
                    self.in_title = False
                    self.title = ""

                def handle_starttag(self, tag, attrs):
                    if tag in ("script", "style", "noscript", "svg"):
                        self.skip += 1
                    if tag == "title":
                        self.in_title = True

                def handle_endtag(self, tag):
                    if tag in ("script", "style", "noscript", "svg") and self.skip > 0:
                        self.skip -= 1
                    if tag == "title":
                        self.in_title = False
                    if tag in ("p", "br", "div", "li", "h1", "h2", "h3", "h4", "tr"):
                        self.parts.append("\n")

                def handle_data(self, data):
                    if self.skip == 0:
                        self.parts.append(data)
                    if self.in_title:
                        self.title += data

            p = _T()
            p.feed(html)
            text = re.sub(r"\n{3,}", "\n\n", "".join(p.parts)).strip()
            title = p.title.strip()
        except Exception as e:
            text = f"[parse failed: {e}]"

    (src_dir / "raw.txt").write_text(
        f"# {title or url}\n\nURL: {url}\n\n{text}", encoding="utf-8")
    (src_dir / "meta.json").write_text(
        json.dumps({"url": url, "title": title,
                    "fetched_at": datetime.now().isoformat()},
                   ensure_ascii=False, indent=2),
        encoding="utf-8")
    return {"kind": "url", "title": title or urlparse(url).netloc,
            "meta": {"url": url, "title": title}}


def ingest_image(path: str, src_dir: Path) -> dict:
    fname = os.path.basename(path)
    dst = src_dir / fname
    shutil.copy2(path, dst)
    (src_dir / "raw.txt").write_text(
        f"# Image: {fname}\n\nLocal source: {path}\nSaved as: {dst}\n\n"
        f"(See image file for content; multi-modal summarization should view it.)",
        encoding="utf-8")
    return {"kind": "image", "title": os.path.splitext(fname)[0],
            "meta": {"image_path": str(dst)}}


def ingest_text(text: str, src_dir: Path) -> dict:
    (src_dir / "raw.txt").write_text(text, encoding="utf-8")
    title = text.strip().splitlines()[0][:30] if text.strip() else "note"
    return {"kind": "text", "title": title, "meta": {}}


# ── ingest subcommand ─────────────────────────────────────────────────────

def cmd_ingest(args, cfg):
    inp = args.input
    kind = detect_kind(inp)
    out_dir = Path(cfg["output_dir"]).expanduser().resolve()
    folder = make_folder(out_dir, kind, args.title or "")
    src_dir = folder / "source"

    if kind == "youtube":
        result = ingest_youtube(inp, src_dir)
    elif kind == "url":
        result = ingest_url(inp, src_dir, fetcher=args.fetcher)
    elif kind == "image":
        result = ingest_image(inp, src_dir)
    else:
        result = ingest_text(inp, src_dir)

    # Improve folder name once we know the real title
    if not args.title and result.get("title"):
        new_slug = slugify(result["title"])
        prefix = folder.name.split("-")[0] + "-" + folder.name.split("-")[1]
        if new_slug:
            new_folder = folder.parent / f"{prefix}-{new_slug}"
            try:
                if new_folder != folder:
                    folder.rename(new_folder)
                    folder = new_folder
                    src_dir = folder / "source"
            except OSError:
                pass

    info = {
        "kind": result["kind"], "title": result.get("title"),
        "input": inp, "folder": str(folder),
        "created_at": datetime.now().isoformat(),
        "meta": result.get("meta", {}),
    }
    (folder / "info.json").write_text(
        json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"FOLDER: {folder}")
    print(f"KIND:   {result['kind']}")
    print(f"TITLE:  {result.get('title')}")
    return folder


# ── AI prompts (concise; tweak to taste) ──────────────────────────────────

_SUMMARY_SYS = (
    "You are a careful study-note writer. Given source material, produce a "
    "Markdown summary following exactly this structure (keep section "
    "headings verbatim, including emojis):\n\n"
    "# <title>\n"
    "> **Source**: <url or file>\n"
    "> **Type**: <youtube|url|image|text>\n\n"
    "## 🎯 One-line summary\n\n"
    "## 📌 Key points (3-5)\n\n"
    "## 🔑 Facts / data\n\n"
    "## 💡 Takeaways\n\n"
    "## 🤔 Open questions\n"
)

_TALKSHOW_SYS = (
    "You write short bilingual-friendly talk-show scripts for two hosts. "
    "Use ONLY the speaker tags I provide, one per line, e.g. `[HostA] …`. "
    "3-5 minutes total, fast pacing, the hosts gently roast each other and "
    "the topic. Keep technical terms verbatim. Plain text, no markdown."
)

_DECK_SYS = (
    "You generate slide decks as JSON. Output STRICT JSON only — no prose, "
    "no code fences. Follow this schema:\n"
    '{ "title": str, "subtitle": str, "slides": ['
    '{"type":"title","title":str,"subtitle":str?},'
    '{"type":"bullets","title":str,"bullets":[str,...]},'
    '{"type":"table","title":str,"headers":[str,...],"rows":[[str,...]]},'
    '{"type":"flow","title":str,"steps":[str,...]},'
    '{"type":"image","title":str?,"image_path":str,"caption":str?},'
    '{"type":"two_column","title":str,"left":str,"image_path":str},'
    '{"type":"summary","title":str,"text":str}'
    "] } "
    "Aim for 5-12 slides total: 1 title, 1-2 overview, 3-6 main "
    "(bullets/table/flow/image mix), 1 summary. Only use image_path values "
    "I list as 'available images' — never invent paths."
)


def _read_source(folder: Path) -> str:
    raw = folder / "source" / "raw.txt"
    return raw.read_text(encoding="utf-8") if raw.exists() else ""


def _list_source_images(folder: Path) -> list[str]:
    src = folder / "source"
    if not src.exists():
        return []
    exts = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".avif"}
    return [str(p) for p in src.iterdir() if p.suffix.lower() in exts]


def _lang_directive(language: str) -> str:
    name = _LANGUAGE_NAMES.get(language, language)
    return (f"\n\nIMPORTANT: Write ALL output (including section headings' "
            f"non-emoji text, body text, bullets, table cells, captions) in "
            f"{name}. Keep proper nouns, code identifiers, and technical "
            f"terms verbatim in their original form.")


def cmd_summarize(args, cfg):
    folder = Path(args.folder).resolve()
    provider = ai_provider.get_provider(cfg["ai"])
    language = _resolve_language(cfg, getattr(args, "language", "") or "")
    src = _read_source(folder)
    info = json.loads((folder / "info.json").read_text(encoding="utf-8"))
    user = (
        f"Source type: {info.get('kind')}\n"
        f"Title hint: {info.get('title')}\n"
        f"Input: {info.get('input')}\n\n"
        f"=== SOURCE START ===\n{src[:60000]}\n=== SOURCE END ==="
    )
    md = provider.complete(_SUMMARY_SYS + _lang_directive(language), user)
    (folder / "summary.md").write_text(md, encoding="utf-8")
    print(f"summary.md: {len(md)} chars (lang={language})")


def cmd_talkshow(args, cfg):
    folder = Path(args.folder).resolve()
    provider = ai_provider.get_provider(cfg["ai"])
    language = _resolve_language(cfg, getattr(args, "language", "") or "")
    voices = _resolve_voices(cfg, language)
    if not voices:
        raise SystemExit("tts.voices is empty in config — add at least two speakers.")
    tags = list(voices.keys())
    summary_md = (folder / "summary.md").read_text(encoding="utf-8") \
        if (folder / "summary.md").exists() else _read_source(folder)
    user = (
        f"Two hosts: {', '.join(tags)}.\n"
        f"Use lines like [{tags[0]}] … and [{tags[1]}] … (only these tags).\n"
        f"Material to discuss:\n\n{summary_md[:30000]}"
    )
    script = provider.complete(_TALKSHOW_SYS + _lang_directive(language), user)
    (folder / "talkshow.txt").write_text(script, encoding="utf-8")
    print(f"talkshow.txt: {len(script)} chars (lang={language}, hosts={tags})")


def cmd_deckplan(args, cfg):
    folder = Path(args.folder).resolve()
    provider = ai_provider.get_provider(cfg["ai"])
    language = _resolve_language(cfg, getattr(args, "language", "") or "")
    summary_md = (folder / "summary.md").read_text(encoding="utf-8") \
        if (folder / "summary.md").exists() else _read_source(folder)
    images = _list_source_images(folder)
    user = (
        f"Available images (use absolute paths verbatim, or omit image slides):\n"
        + ("\n".join(f"- {p}" for p in images) if images else "(none)")
        + f"\n\nSummary / source to base the deck on:\n\n{summary_md[:30000]}"
    )
    spec = ai_provider.complete_json(provider, _DECK_SYS + _lang_directive(language), user)
    (folder / "deck.json").write_text(
        json.dumps(spec, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"deck.json: {len(spec.get('slides', []))} slides (lang={language})")


def cmd_tts(args, cfg):
    language = _resolve_language(cfg, getattr(args, "language", "") or "")
    voices_cfg = _resolve_voices(cfg, language)
    voices = {k: (v.get("voice"), v.get("rate", "+0%"))
              for k, v in voices_cfg.items() if v.get("voice")}
    if not voices:
        raise SystemExit("tts.voices is empty in config — add at least one speaker.")
    text = Path(args.script).read_text(encoding="utf-8")
    build_dual_audio(text, args.output, voices,
                     use_jingle=cfg["tts"].get("jingle", True),
                     custom_jingle_mp3=cfg["tts"].get("custom_jingle_mp3", ""))


def cmd_ppt(args, cfg):
    build_pptx(args.json, args.output)


def cmd_publish(args, cfg):
    folder = Path(args.folder).resolve()
    info = json.loads((folder / "info.json").read_text(encoding="utf-8")) \
        if (folder / "info.json").exists() else {}
    slug = folder.name

    one_liner = ""
    sm = folder / "summary.md"
    title = info.get("title") or slug
    if sm.exists():
        md = sm.read_text(encoding="utf-8")
        m = re.search(r"##\s*🎯[^\n]*\n+([^\n#]+)", md)
        if m:
            one_liner = m.group(1).strip()
        else:
            for ln in md.splitlines():
                s = ln.strip()
                if s and not s.startswith("#") and not s.startswith(">"):
                    one_liner = s
                    break

    links: Dict[str, str] = {}
    local_links: Dict[str, str] = {}
    for label, fname in [("🎧 Listen", "talkshow.mp3"),
                         ("📊 Slides", "deck.pptx"),
                         ("📝 Summary", "summary.md")]:
        fp = folder / fname
        if fp.exists():
            local_links[label] = fp.as_uri()

    # Upload to cloud (OneDrive / Google Drive / S3 / etc. via rclone) and
    # prefer the shareable URLs over local file:// when available.
    upload_cfg = cfg.get("upload") or {}
    cloud_links: Dict[str, str] = {}
    try:
        cloud_links = upload_mod.upload_folder(folder, upload_cfg)
    except Exception as e:  # noqa: BLE001
        print(f"WARN upload failed: {e}")

    for label, uri in local_links.items():
        links[label] = cloud_links.get(label) or uri

    (folder / "links.json").write_text(
        json.dumps({"title": title, "slug": slug, "summary": one_liner,
                    "links": links, "local_links": local_links,
                    "cloud_links": cloud_links}, ensure_ascii=False, indent=2),
        encoding="utf-8")

    n = cfg.get("notify") or {}
    try:
        notify.post(n.get("webhook_url"), n.get("style", "generic"),
                    title, one_liner, links)
    except Exception as e:
        print(f"WARN notify failed: {e}")

    print(f"PUBLISH DONE — {folder}")
    print(json.dumps(links, ensure_ascii=False, indent=2))


def cmd_auto(args, cfg):
    folder = cmd_ingest(args, cfg)

    if cfg["ai"].get("provider", "none") == "none":
        print("ai.provider is 'none' — stopping after ingest.")
        return

    lang_override = getattr(args, "language", "") or ""

    class _Ns:
        pass

    ns = _Ns()
    ns.folder = str(folder)
    ns.language = lang_override
    cmd_summarize(ns, cfg)

    if cfg["tts"].get("enabled", True):
        cmd_talkshow(ns, cfg)
        tts_ns = _Ns()
        tts_ns.script = str(folder / "talkshow.txt")
        tts_ns.output = str(folder / "talkshow.mp3")
        tts_ns.language = lang_override
        cmd_tts(tts_ns, cfg)

    cmd_deckplan(ns, cfg)
    ppt_ns = _Ns()
    ppt_ns.json = str(folder / "deck.json")
    ppt_ns.output = str(folder / "deck.pptx")
    ppt_ns.language = lang_override
    cmd_ppt(ppt_ns, cfg)

    pub_ns = _Ns()
    pub_ns.folder = str(folder)
    pub_ns.language = lang_override
    cmd_publish(pub_ns, cfg)


# ── CLI plumbing ──────────────────────────────────────────────────────────

def main():
    ap = argparse.ArgumentParser(prog="yumnb")
    ap.add_argument("--config", default="", help="Path to config.yaml")
    ap.add_argument("--language", default="",
                    help="Override output language (e.g. en|zh|ja|es|fr|de). "
                         "Defaults to config.language or 'en'. Selects both "
                         "AI prompt language and the default TTS voice pair.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest")
    p.add_argument("input"); p.add_argument("--title", default="")
    p.add_argument("--fetcher", default="",
                   help="Optional external command (e.g. 'python my_fetch.py') used to "
                        "fetch JS/auth-walled pages when plain requests gets HTML-less response.")
    p.set_defaults(func=cmd_ingest)

    p = sub.add_parser("summarize"); p.add_argument("folder"); p.set_defaults(func=cmd_summarize)
    p = sub.add_parser("talkshow"); p.add_argument("folder"); p.set_defaults(func=cmd_talkshow)
    p = sub.add_parser("deckplan"); p.add_argument("folder"); p.set_defaults(func=cmd_deckplan)

    p = sub.add_parser("tts")
    p.add_argument("script"); p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_tts)

    p = sub.add_parser("ppt")
    p.add_argument("json"); p.add_argument("--output", required=True)
    p.set_defaults(func=cmd_ppt)

    p = sub.add_parser("publish"); p.add_argument("folder"); p.set_defaults(func=cmd_publish)

    p = sub.add_parser("auto")
    p.add_argument("input"); p.add_argument("--title", default="")
    p.add_argument("--fetcher", default="")
    p.set_defaults(func=cmd_auto)

    args = ap.parse_args()
    cfg = load_config(args.config)
    args.func(args, cfg)


if __name__ == "__main__":
    main()

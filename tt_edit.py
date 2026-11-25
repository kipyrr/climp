import os
import subprocess
from pathlib import Path

HERE = Path(__file__).resolve().parent
PHRASES_FILE = HERE / "phrases.txt"   # one word/phrase per line: hero names, slang, your catchphrases
NO_WINDOW = 0x08000000 if os.name == "nt" else 0

BASE_FILTER = (
    "[0:v]split=4[a][b][c][d];"
    "[a]scale=540:960:force_original_aspect_ratio=increase,crop=540:960,"
    "boxblur=12:2,scale=1080:1920[bg];"
    "[b]crop=1263:1073:310:1,scale=1080:918:flags=lanczos[fg];"
    "[c]crop=380:86:1505:28,scale=494:112:flags=lanczos[kf];"
    "[d]crop=178:49:44:156,scale=266:74:flags=lanczos[vc];"
    "[bg][fg]overlay=0:501[t1];"
    "[t1][kf]overlay=496:377[t2];"
    "[t2][vc]overlay=40:412,fps=60"
)

# ---------- captions ----------

def _phrases():
    if not PHRASES_FILE.exists():
        return None
    lines = [l.strip() for l in PHRASES_FILE.read_text(encoding="utf-8").splitlines()]
    words = [l for l in lines if l and not l.startswith("#")]
    return ", ".join(words) or None


def transcribe(src):
    """Speech -> list of words with timestamps. GPU first, CPU fallback."""
    from faster_whisper import WhisperModel
    prompt = _phrases()
    last = None
    for device, compute in (("cuda", "float16"), ("cpu", "int8")):
        try:
            model = WhisperModel("small", device=device, compute_type=compute)
            segments, _ = model.transcribe(
                str(src), language="en", word_timestamps=True,
                vad_filter=True, initial_prompt=prompt)
            return [w for s in segments for w in (s.words or [])]
        except Exception as e:
            last = e
    raise last


def chunk(words, max_words=3, max_gap=0.6):
    """Group words into short TikTok-style caption pops."""
    groups, cur = [], []
    for w in words:
        if cur and (len(cur) >= max_words or w.start - cur[-1].end > max_gap):
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    return [(g[0].start, g[-1].end, " ".join(x.word.strip() for x in g)) for g in groups]


def _srt_time(t):
    ms = int(round(t * 1000))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02}:{m:02}:{s:02},{ms:03}"


def _parse_srt_time(s):
    hms, ms = s.strip().split(",")
    h, m, sec = hms.split(":")
    return int(h) * 3600 + int(m) * 60 + int(sec) + int(ms) / 1000


def write_srt(path, cues):
    out = []
    for i, (a, b, text) in enumerate(cues, 1):
        out.append(f"{i}\n{_srt_time(a)} --> {_srt_time(b)}\n{text}\n")
    Path(path).write_text("\n".join(out), encoding="utf-8")


def read_srt(path):
    cues = []
    for block in Path(path).read_text(encoding="utf-8").strip().split("\n\n"):
        lines = [l for l in block.strip().splitlines() if l.strip()]
        if len(lines) >= 2 and "-->" in lines[1]:
            a, b = lines[1].split("-->")
            cues.append((_parse_srt_time(a), _parse_srt_time(b), " ".join(lines[2:])))
    return cues


def _ass_time(t):
    cs = int(round(t * 100))
    h, cs = divmod(cs, 360_000)
    m, cs = divmod(cs, 6000)
    s, cs = divmod(cs, 100)
    return f"{h}:{m:02}:{s:02}.{cs:02}"


def write_ass(path, cues):
    """Bold white captions with black outline, in the lower blurred band."""
    head = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\nWrapStyle: 0\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, "
        "BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, "
        "BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding\n"
        "Style: Cap,Arial Black,84,&H00FFFFFF,&H000000FF,&H00000000,&H64000000,"
        "-1,0,0,0,100,100,0,0,1,6,2,2,80,80,330,1\n\n"
        "[Events]\nFormat: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    rows = []
    for a, b, text in cues:
        clean = text.upper().replace("\\", "/").replace("{", "(").replace("}", ")")
        rows.append(f"Dialogue: 0,{_ass_time(a)},{_ass_time(b)},Cap,,0,0,0,,{clean}")
    Path(path).write_text(head + "\n".join(rows) + "\n", encoding="utf-8")

# ---------- edit ----------

def make_tiktok(src, dst=None, use_nvenc: bool = True, captions: bool = True) -> Path:
    src = Path(src).resolve()
    dst = Path(dst).resolve() if dst else src.with_name(src.stem + "_tiktok.mp4")
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.with_name(dst.stem + ".part.mp4")
    srt = dst.with_suffix(".srt")   # the reviewable caption file

    cues = None
    if captions:
        try:
            if srt.exists():
                cues = read_srt(srt)          # use your fixed version
            else:
                cues = chunk(transcribe(src))
                write_srt(srt, cues)
        except Exception:
            cues = None                       # no captions, but still make the clip

    vf = BASE_FILTER
    if cues:
        write_ass(dst.parent / "_burn.ass", cues)
        vf += ",subtitles=_burn.ass"
    vf += ",format=yuv420p[v]"

    if use_nvenc:
        video = ["-c:v", "h264_nvenc", "-preset", "p7", "-rc", "vbr",
                 "-cq", "18", "-b:v", "40M", "-maxrate", "50M", "-bufsize", "80M"]
    else:
        video = ["-c:v", "libx264", "-preset", "slow", "-crf", "17"]

    cmd = ["ffmpeg", "-y", "-i", str(src),
           "-filter_complex", vf, "-map", "[v]", "-map", "0:a?",
           *video, "-c:a", "aac", "-b:a", "320k",
           "-movflags", "+faststart", str(tmp)]
    subprocess.run(cmd, check=True, cwd=str(dst.parent),
                   stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                   stderr=subprocess.PIPE, creationflags=NO_WINDOW)
    os.replace(tmp, dst)
    return dst


if __name__ == "__main__":
    import sys
    print(make_tiktok(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None))
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import threading
import uuid
import zipfile
from flask import Flask, jsonify, request, send_file, send_from_directory
from flask_cors import CORS
from werkzeug.utils import secure_filename

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
OUTPUT_DIR = os.path.join(BASE_DIR, "clips_output")
CACHE_DIR = os.path.join(BASE_DIR, "video_cache")
os.makedirs(OUTPUT_DIR, exist_ok=True)
os.makedirs(CACHE_DIR, exist_ok=True)

MAX_CACHE_FILES = 12
MAX_CACHE_BYTES = 8 * 1024 * 1024 * 1024  # 8 GB

app = Flask(__name__, static_folder=BASE_DIR, static_url_path="")
CORS(app)

JOBS = {}
JOBS_LOCK = threading.Lock()
HW_ENCODER_CACHE = None

ASPECT_MAP = {
    "9:16": (1080, 1920),
    "4:5": (1080, 1350),
    "1:1": (1080, 1080),
    "4:3": (1440, 1080),
    "16:9": (1920, 1080),
}

QUALITY_PRESETS = {
    "1080": {"max_h": 1080, "crf": "18", "preset": "fast", "audio_br": "192k"},
    "720": {"max_h": 720, "crf": "20", "preset": "veryfast", "audio_br": "160k"},
    "480": {"max_h": 480, "crf": "22", "preset": "veryfast", "audio_br": "128k"},
}


def detect_hw_encoder() -> str:
  global HW_ENCODER_CACHE
  if HW_ENCODER_CACHE is not None:
    return HW_ENCODER_CACHE
  try:
    res = subprocess.run(
        ["ffmpeg", "-hide_banner", "-encoders"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    out = res.stdout or ""
    for enc in ("h264_nvenc", "h264_qsv", "h264_amf"):
      if enc in out:
        HW_ENCODER_CACHE = enc
        return enc
  except Exception:
    pass
  HW_ENCODER_CACHE = "libx264"
  return HW_ENCODER_CACHE


def build_video_encode_args(encoder: str, q_cfg: dict) -> list:
  if encoder == "h264_nvenc":
    return [
        "-c:v",
        "h264_nvenc",
        "-preset",
        "p4",
        "-cq",
        q_cfg["crf"],
        "-pix_fmt",
        "yuv420p",
    ]
  if encoder == "h264_qsv":
    return [
        "-c:v",
        "h264_qsv",
        "-global_quality",
        q_cfg["crf"],
        "-pix_fmt",
        "nv12",
    ]
  if encoder == "h264_amf":
    return [
        "-c:v",
        "h264_amf",
        "-quality",
        "balanced",
        "-pix_fmt",
        "yuv420p",
    ]
  return [
      "-c:v",
      "libx264",
      "-preset",
      q_cfg["preset"],
      "-crf",
      q_cfg["crf"],
      "-pix_fmt",
      "yuv420p",
  ]


def get_dir_size_mb(folder: str) -> float:
  total = 0
  try:
    for f in os.listdir(folder):
      p = os.path.join(folder, f)
      if os.path.isfile(p):
        total += os.path.getsize(p)
  except Exception:
    pass
  return round(total / (1024 * 1024), 1)


def enforce_cache_limit():
  try:
    files = []
    total_size = 0
    for f in os.listdir(CACHE_DIR):
      p = os.path.join(CACHE_DIR, f)
      if os.path.isfile(p):
        sz = os.path.getsize(p)
        mt = os.path.getmtime(p)
        files.append((mt, sz, p))
        total_size += sz
    files.sort(key=lambda x: x[0])
    while files and (
        len(files) > MAX_CACHE_FILES or total_size > MAX_CACHE_BYTES
    ):
      _, sz, old_path = files.pop(0)
      try:
        os.remove(old_path)
        total_size -= sz
      except Exception:
        pass
  except Exception:
    pass


def get_lan_ip() -> str:
  try:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.connect(("8.8.8.8", 80))
    ip = s.getsockname()[0]
    s.close()
    if ip and not ip.startswith("127."):
      return ip
  except Exception:
    pass
  return "127.0.0.1"


def get_base_url() -> str:
  host = request.host or "127.0.0.1:5000"
  if host.startswith("127.0.0.1") or host.startswith("localhost"):
    lan = get_lan_ip()
    if lan != "127.0.0.1":
      port = host.split(":")[1] if ":" in host else "5000"
      return f"http://{lan}:{port}"
  return f"http://{host}"


def set_job_status(
    job_id: str,
    step: str,
    detail: str,
    done: bool = False,
    error: str = "",
    clips=None,
    source_title: str = "",
):
  with JOBS_LOCK:
    prev = JOBS.get(job_id, {})
    JOBS[job_id] = {
        "job_id": job_id,
        "step": step,
        "detail": detail,
        "done": done,
        "error": error,
        "clips": clips if clips is not None else prev.get("clips", []),
        "source_title": source_title or prev.get("source_title", ""),
    }


def normalize_quality(q: str) -> str:
  q_clean = str(q or "720").replace("p", "").strip()
  return q_clean if q_clean in QUALITY_PRESETS else "720"


def get_scaled_dimensions(ratio: str, quality: str):
  base_w, base_h = ASPECT_MAP.get(ratio, (1080, 1920))
  q = normalize_quality(quality)
  max_h = QUALITY_PRESETS[q]["max_h"]

  if ratio == "16:9":
    target_h = min(base_h, max_h)
    target_w = int(round(target_h * 16 / 9 / 2)) * 2
    return target_w, target_h
  if ratio == "4:3":
    target_h = min(base_h, max_h)
    target_w = int(round(target_h * 4 / 3 / 2)) * 2
    return target_w, target_h
  if ratio == "1:1":
    side = min(base_w, max_h)
    side = int(round(side / 2)) * 2
    return side, side
  if ratio == "4:5":
    target_h = min(base_h, int(round(max_h * 1.25 / 2)) * 2)
    target_w = int(round(target_h * 4 / 5 / 2)) * 2
    return target_w, target_h

  vert_h_map = {"1080": 1920, "720": 1280, "480": 854}
  target_h = vert_h_map.get(q, 1280)
  target_w = int(round(target_h * 9 / 16 / 2)) * 2
  return target_w, target_h


def get_url_hash(url: str) -> str:
  return hashlib.md5(url.strip().encode("utf-8")).hexdigest()[:12]


def get_cached_raw_video(url: str, quality: str = "720") -> str:
  if url.startswith("local://"):
    fname = os.path.basename(url.replace("local://", ""))
    local_path = os.path.join(CACHE_DIR, fname)
    if os.path.exists(local_path):
      return local_path
    raise FileNotFoundError("Локальный исходник не найден в кэше")

  q = normalize_quality(quality)
  max_h = QUALITY_PRESETS[q]["max_h"]
  h = get_url_hash(url)
  cached_path = os.path.join(CACHE_DIR, f"src_{h}_{q}p.mp4")
  if os.path.exists(cached_path) and os.path.getsize(cached_path) > 1024 * 100:
    return cached_path

  enforce_cache_limit()
  tmp_path = os.path.join(CACHE_DIR, f"tmp_{h}_{q}p.mp4")
  fmt_selector = (
      f"bestvideo[height<={max_h}][ext=mp4]+bestaudio[ext=m4a]/"
      f"bestvideo[height<={max_h}]+bestaudio/"
      f"best[height<={max_h}][ext=mp4]/best[height<={max_h}]/best"
  )
  subprocess.run(
      [
          "yt-dlp",
          "--no-playlist",
          "-f",
          fmt_selector,
          "--merge-output-format",
          "mp4",
          "-o",
          tmp_path,
          url,
      ],
      check=True,
  )

  actual_file = tmp_path
  if not os.path.exists(actual_file):
    candidates = [
        os.path.join(CACHE_DIR, f)
        for f in os.listdir(CACHE_DIR)
        if f.startswith(f"tmp_{h}_{q}p")
    ]
    if candidates:
      actual_file = candidates[0]

  if os.path.exists(cached_path):
    os.remove(cached_path)
  os.replace(actual_file, cached_path)
  enforce_cache_limit()
  return cached_path


def get_video_meta(url: str):
  if url.startswith("local://"):
    fname = os.path.basename(url.replace("local://", ""))
    clean_name = re.sub(r"^local_[a-f0-9]+_", "", fname).rsplit(".", 1)[0]
    return clean_name, ["#гейминг", "#стрим", "#нарезка"]

  title = ""
  tags = []
  try:
    res = subprocess.run(
        ["yt-dlp", "--no-playlist", "--dump-single-json", "--no-warnings", url],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if res.returncode == 0 and res.stdout:
      info = json.loads(res.stdout)
      title = (info.get("title") or "").strip()
      raw_tags = info.get("tags") or []
      for t in raw_tags[:5]:
        cleaned = re.sub(r"[^0-9A-Za-zА-Яа-яЁё_]", "", str(t))
        if cleaned:
          tags.append("#" + cleaned.lower())
  except Exception:
    pass
  return title, tags


def build_topic_and_hashtags(
    source_title,
    idx,
    start_sec,
    peak_db,
    yt_tags,
    ratio,
    niche="gaming",
    cut_strategy="hype",
):
  clean_src = re.sub(r"\[[^\]]*\]|\([^\)]*\)", "", source_title or "").strip()
  clean_src = re.sub(r"\s+", " ", clean_src)
  short_src = clean_src[:50] + ("..." if len(clean_src) > 50 else "")

  low_title = clean_src.lower()
  extra_tags = []
  hook_prefix = "Яркий момент"

  if niche == "cinema":
    extra_tags.extend(["#кино", "#разбор", "#фильмы", "#сцена"])
    hook_prefix = "Разбор сцены"
  elif niche == "traffic":
    extra_tags.extend(["#рек", "#тренды", "#вирусное", "#шортс"])
    hook_prefix = "Момент с удержанием"
  else:
    if any(w in low_title for w in ["gta 6", "gta vi", "гта 6", "гта ви"]):
      extra_tags.extend(["#gta6", "#gtavi", "#гта6"])
      hook_prefix = "GTA 6 момент"
    elif any(w in low_title for w in ["gta", "гта", "вайс сити", "vice city"]):
      extra_tags.extend(["#gta", "#гта", "#гейминг"])
      hook_prefix = "Геймплейный момент"
    elif any(w in low_title for w in ["стрим", "реакц", "смотрит", "нарезк"]):
      extra_tags.extend(["#стрим", "#реакция", "#нарезки"])
      hook_prefix = "Реакция на стриме"
    else:
      extra_tags.extend(["#гейминг", "#тренды", "#топмомент"])

  if cut_strategy == "series":
    hook_prefix = f"Часть {idx + 1}"
  elif cut_strategy == "around":
    hook_prefix = f"Вариант #{idx + 1}"
  elif peak_db is not None and peak_db > -14:
    hook_prefix = f"Разрывной момент ({int(peak_db)}dB)"

  mm, ss = start_sec // 60, start_sec % 60
  base_label = short_src or f"Клип #{idx + 1}"

  if cut_strategy == "series":
    title_variants = [
        f"Часть {idx + 1} • {base_label} [{mm}:{ss:02d}]",
        f"{base_label} — Продолжение (Часть {idx + 1})",
        f"Смотри с начала! Часть {idx + 1}: {base_label}",
    ]
  elif cut_strategy == "around":
    title_variants = [
        f"Заход #{idx + 1} • {base_label} [{mm}:{ss:02d}]",
        f"Спорный момент в {base_label} ({mm}:{ss:02d})",
        f"Факт: что случилось на {mm}:{ss:02d} в {base_label}",
    ]
  else:
    title_variants = [
        f"{hook_prefix} • {base_label} [{mm}:{ss:02d}]",
        f"Досмотри до конца! {base_label} ({mm}:{ss:02d})",
        f"Никто не ожидал такого в {base_label} • #{idx + 1}",
    ]

  platform_tags = (
      ["#tiktok", "#reels", "#shorts", "#рек", "#рекомендации"]
      if ratio in ("9:16", "4:5", "1:1")
      else ["#youtube", "#shorts", "#гейминг", "#рек"]
  )

  merged = []
  for tag in extra_tags + yt_tags + platform_tags:
    t_low = tag.lower()
    if len(t_low) > 2 and t_low not in merged:
      merged.append(t_low)
    if len(merged) >= 8:
      break

  return title_variants[0], title_variants, " ".join(merged)


def find_series_timestamps(total_dur, count, clip_dur, range_start=None, range_end=None):
  win_start = 0.0 if range_start is None else max(0.0, float(range_start))
  win_end = total_dur if range_end is None else min(total_dur, float(range_end))
  if win_end - win_start < clip_dur:
    win_start = 0.0
    win_end = total_dur

  parts = []
  cur = int(win_start)
  for _ in range(count):
    if cur + clip_dur <= int(win_end) and cur + clip_dur <= total_dur:
      parts.append((cur, -15.0))
      cur += clip_dur
    else:
      break
  return parts or [(int(win_start), -15.0)]


def find_around_timestamps(total_dur, count, clip_dur, anchor_sec):
  center = max(0, min(max(0, int(total_dur) - clip_dur), int(anchor_sec or 0)))
  offsets = [-8, 0, 5, 12, -15, 20]
  spots = []
  for off in offsets:
    st = max(0, min(max(0, int(total_dur) - clip_dur), center + off))
    if all(abs(st - s[0]) >= 3 for s in spots):
      spots.append((st, -13.0))
    if len(spots) >= count:
      break
  return sorted(spots, key=lambda x: x[0]) or [(center, -13.0)]


def find_hype_timestamps(
    video_path, total_dur, count, clip_dur, range_start=None, range_end=None
):
  win_start = 0.0 if range_start is None else max(0.0, float(range_start))
  win_end = total_dur if range_end is None else min(total_dur, float(range_end))
  if win_end - win_start < clip_dur:
    win_start = 0.0
    win_end = total_dur

  peaks = []
  try:
    cmd = [
        "ffmpeg",
        "-ss",
        str(win_start),
        "-to",
        str(win_end),
        "-i",
        video_path,
        "-af",
        "astats=metadata=1:reset=1,ametadata=print:key=lavfi.astats.Overall.RMS_level",
        "-f",
        "null",
        "-",
    ]
    res = subprocess.run(cmd, stderr=subprocess.PIPE, text=True, timeout=60)
    cur_time = win_start
    for line in res.stderr.splitlines():
      t_match = re.search(r"pts_time:([0-9.]+)", line)
      if t_match:
        cur_time = win_start + float(t_match.group(1))
      rms_match = re.search(r"RMS_level=(-?[0-9.]+)", line)
      if rms_match:
        val = float(rms_match.group(1))
        if val > -60:
          peaks.append((val, cur_time))
    peaks.sort(reverse=True, key=lambda x: x[0])
    selected = []
    for db_val, t in peaks:
      start = max(int(win_start), int(t - clip_dur * 0.35))
      if start + clip_dur > int(win_end):
        start = max(int(win_start), int(win_end) - clip_dur)
      if start + clip_dur > total_dur:
        continue
      if all(abs(start - s[0]) >= clip_dur * 0.85 for s in selected):
        selected.append((start, round(db_val, 1)))
      if len(selected) >= count:
        break
    if len(selected) >= count:
      return sorted(selected, key=lambda x: x[0])
  except Exception:
    pass

  span = max(float(clip_dur), win_end - win_start)
  step = max(clip_dur, (span - clip_dur) / max(1, count))
  fallback = []
  for i in range(count):
    st = int(win_start + i * step)
    if st + clip_dur <= int(win_end) and st + clip_dur <= total_dur:
      fallback.append((st, -18.0))
  return fallback or [(int(win_start), -18.0)]


def has_audio_stream(video_path: str) -> bool:
  try:
    out = subprocess.check_output(
        [
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "a",
            "-show_entries",
            "stream=index",
            "-of",
            "csv=p=0",
            video_path,
        ],
        text=True,
        timeout=10,
    )
    return bool(out.strip())
  except Exception:
    return False


def clamp01(val, default):
  try:
    v = float(val)
    return max(0.0, min(1.0, v))
  except Exception:
    return default


def escape_drawtext(text: str) -> str:
  cleaned = re.sub(r"[\r\n]+", " ", (text or "").strip())
  cleaned = cleaned.replace("\\", "").replace(":", "\\:").replace("'", "\\'")
  return cleaned[:70]


def find_system_font() -> str:
  candidates = [
      "C:/Windows/Fonts/arialbd.ttf",
      "C:/Windows/Fonts/DejaVuSans-Bold.ttf",
      "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
  ]
  for path in candidates:
    if os.path.exists(path):
      return path
  return ""


def render_single_segment(
    src_video: str,
    out_path: str,
    start_sec: int,
    duration: int,
    params: dict,
    total_dur: float = 99999.0,
):
  mode = (params.get("mode") or "webcam").strip().lower()
  quality = normalize_quality(params.get("quality", "720"))
  q_cfg = QUALITY_PRESETS[quality]
  ratio = params.get("aspect_ratio", "9:16")
  pan_x = clamp01(params.get("pan_x"), 0.5)
  normalize_audio = bool(params.get("normalize_audio", True))
  cold_start = bool(params.get("cold_start", False))
  dynamic_zoom = bool(params.get("dynamic_zoom", False))
  hook_text = escape_drawtext(params.get("hook_text") or "")
  hook_pos = "bottom" if (params.get("hook_pos") == "bottom") else "top"

  split = max(0.15, min(0.65, float(params.get("split_ratio", 0.30))))
  raw_cam = params.get("cam") or {}
  raw_game = params.get("game") or {}

  cam_x = clamp01(raw_cam.get("x"), 0.02)
  cam_y = clamp01(raw_cam.get("y"), 0.65)
  cam_w = max(0.05, min(1.0 - cam_x, clamp01(raw_cam.get("w"), 0.28)))
  cam_h = max(0.05, min(1.0 - cam_y, clamp01(raw_cam.get("h"), 0.30)))

  game_x = clamp01(raw_game.get("x"), 0.0)
  game_y = clamp01(raw_game.get("y"), 0.0)
  game_w = max(0.10, min(1.0 - game_x, clamp01(raw_game.get("w"), 1.0)))
  game_h = max(0.10, min(1.0 - game_y, clamp01(raw_game.get("h"), 1.0)))

  out_w, out_h = get_scaled_dimensions(ratio, quality)
  font_path = find_system_font()

  if mode == "clean":
    crop_x_expr = f"(iw-ow)*{pan_x:.3f}"
    base_filter = (
        f"[v_src]scale=-2:{out_h}:force_original_aspect_ratio=increase,"
        f"crop={out_w}:{out_h}:{crop_x_expr}:(ih-oh)/2[v_raw]"
    )
  elif ratio in ("16:9", "4:3"):
    pip_w = int(round(out_w * 0.26 / 2)) * 2
    pip_h = int(round(out_h * 0.30 / 2)) * 2
    base_filter = (
        f"[v_src]crop=iw*{game_w}:ih*{game_h}:iw*{game_x}:ih*{game_y},scale={out_w}:{out_h}[bg];"
        f"[v_src]crop=iw*{cam_w}:ih*{cam_h}:iw*{cam_x}:ih*{cam_y},scale={pip_w}:{pip_h}[pip];"
        f"[bg][pip]overlay=24:{out_h - pip_h - 24}[v_raw]"
    )
  else:
    top_h = int(round(out_h * split / 2)) * 2
    bot_h = out_h - top_h
    base_filter = (
        f"[v_src]crop=iw*{cam_w}:ih*{cam_h}:iw*{cam_x}:ih*{cam_y},scale={out_w}:{top_h}:force_original_aspect_ratio=increase,crop={out_w}:{top_h}[top];"
        f"[v_src]crop=iw*{game_w}:ih*{game_h}:iw*{game_x}:ih*{game_y},scale={out_w}:{bot_h}:force_original_aspect_ratio=increase,crop={out_w}:{bot_h}[bot];"
        "[top][bot]vstack=inputs=2[v_raw]"
    )

  if dynamic_zoom:
    zoom_expr = "1.0+0.05*sin(t*1.4)"
    zoom_filter = (
        f"[v_raw]scale=iw*({zoom_expr}):ih*({zoom_expr}):eval=frame,"
        f"crop={out_w}:{out_h}:(iw-{out_w})/2:(ih-{out_h})/2[v_mid]"
    )
  else:
    zoom_filter = "[v_raw]null[v_mid]"

  if hook_text:
    box_h = max(56, int(out_h * 0.065))
    font_sz = max(24, int(box_h * 0.48))
    y_box = 0 if hook_pos == "top" else f"ih-{box_h}"
    y_txt = (
        f"({box_h}-text_h)/2"
        if hook_pos == "top"
        else f"ih-{box_h}+({box_h}-text_h)/2"
    )
    font_opt = (
        f":fontfile='{font_path.replace(chr(92), '/')}'" if font_path else ""
    )
    tail_filter = (
        f"{zoom_filter};"
        f"[v_mid]drawbox=x=0:y={y_box}:w=iw:h={box_h}:color=black@0.65:t=fill,"
        f"drawtext=text='{hook_text}'{font_opt}:fontcolor=white:fontsize={font_sz}:"
        f"x=(w-text_w)/2:y={y_txt}:borderw=2:bordercolor=black@0.85[v]"
    )
  else:
    tail_filter = f"{zoom_filter};[v_mid]null[v]"

  encoder = detect_hw_encoder()
  enc_args = build_video_encode_args(encoder, q_cfg)
  audio_present = has_audio_stream(src_video)

  teaser_sec = 2
  teaser_start = min(
      max(0.0, total_dur - teaser_sec - 0.5),
      float(start_sec) + float(duration) * 0.65,
  )
  can_teaser = (
      cold_start and duration > 8 and teaser_start + teaser_sec < total_dur
  )

  def build_cmd(v_args):
    if can_teaser:
      main_dur = max(4, duration - teaser_sec)
      if audio_present:
        fc = (
            f"[0:v]trim=start={teaser_start:.2f}:duration={teaser_sec},setpts=PTS-STARTPTS[tv];"
            f"[0:a]atrim=start={teaser_start:.2f}:duration={teaser_sec},asetpts=PTS-STARTPTS[ta];"
            f"[0:v]trim=start={start_sec}:duration={main_dur},setpts=PTS-STARTPTS[mv];"
            f"[0:a]atrim=start={start_sec}:duration={main_dur},asetpts=PTS-STARTPTS[ma];"
            f"[tv][ta][mv][ma]concat=n=2:v=1:a=1[v_src][a_cat];"
            f"{base_filter};{tail_filter};"
            + (
                "[a_cat]loudnorm=I=-16:TP=-1.5:LRA=11[a_out]"
                if normalize_audio
                else "[a_cat]anull[a_out]"
            )
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            src_video,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
            "-map",
            "[a_out]",
        ]
      else:
        fc = (
            f"[0:v]trim=start={teaser_start:.2f}:duration={teaser_sec},setpts=PTS-STARTPTS[tv];"
            f"[0:v]trim=start={start_sec}:duration={main_dur},setpts=PTS-STARTPTS[mv];"
            f"[tv][mv]concat=n=2:v=1:a=0[v_src];"
            f"{base_filter};{tail_filter}"
        )
        cmd = [
            "ffmpeg",
            "-y",
            "-i",
            src_video,
            "-filter_complex",
            fc,
            "-map",
            "[v]",
        ]
    else:
      fc = f"[0:v]null[v_src];{base_filter};{tail_filter}"
      cmd = [
          "ffmpeg",
          "-y",
          "-ss",
          str(start_sec),
          "-t",
          str(duration),
          "-i",
          src_video,
          "-filter_complex",
          fc,
          "-map",
          "[v]",
      ]
      if audio_present:
        cmd.extend(["-map", "0:a"])
        if normalize_audio:
          cmd.extend(["-af", "loudnorm=I=-16:TP=-1.5:LRA=11"])

    cmd.extend(v_args)
    cmd.extend(["-movflags", "+faststart"])
    if audio_present:
      cmd.extend(["-c:a", "aac", "-b:a", q_cfg["audio_br"]])
    cmd.append(out_path)
    return cmd

  try:
    subprocess.run(build_cmd(enc_args), check=True)
  except subprocess.CalledProcessError:
    fallback_args = build_video_encode_args("libx264", q_cfg)
    subprocess.run(build_cmd(fallback_args), check=True)


@app.route("/", methods=["GET"])
def home():
  return send_from_directory(BASE_DIR, "index.html")


@app.route("/ping", methods=["GET"])
def ping():
  return jsonify({
      "status": "ok",
      "brand": "Kl1ni Clips",
      "lan_ip": get_lan_ip(),
      "encoder": detect_hw_encoder(),
      "cache_mb": get_dir_size_mb(CACHE_DIR),
      "output_mb": get_dir_size_mb(OUTPUT_DIR),
  })


@app.route("/api/open-folder", methods=["POST"])
def open_local_folder():
  data = request.json or {}
  target = (data.get("target") or "output").strip().lower()
  folder_path = CACHE_DIR if target == "cache" else OUTPUT_DIR
  try:
    if os.name == "nt":
      os.startfile(folder_path)
    return jsonify({"opened": folder_path})
  except Exception as e:
    return jsonify({"error": str(e)}), 500


@app.route("/api/cache/clear", methods=["POST"])
def clear_cache():
  removed = 0
  freed_bytes = 0
  for fname in os.listdir(CACHE_DIR):
    fpath = os.path.join(CACHE_DIR, fname)
    if os.path.isfile(fpath):
      try:
        freed_bytes += os.path.getsize(fpath)
        os.remove(fpath)
        removed += 1
      except Exception:
        pass
  return jsonify({
      "removed": removed,
      "freed_mb": round(freed_bytes / (1024 * 1024), 1),
  })


@app.route("/api/upload-local", methods=["POST"])
def upload_local_video():
  if "file" not in request.files:
    return jsonify({"error": "Файл не передан"}), 400
  f = request.files["file"]
  if not f or not f.filename:
    return jsonify({"error": "Пустое имя файла"}), 400

  enforce_cache_limit()
  safe = secure_filename(f.filename) or "video.mp4"
  uid = str(uuid.uuid4())[:8]
  saved_name = f"local_{uid}_{safe}"
  saved_path = os.path.join(CACHE_DIR, saved_name)
  f.save(saved_path)

  stream_url = f"{get_base_url()}/cache/{saved_name}"
  return jsonify({
      "local_ref": f"local://{saved_name}",
      "title": f.filename,
      "stream_url": stream_url,
  })


@app.route("/api/rerender-clip", methods=["POST"])
def rerender_single_clip():
  data = request.json or {}
  url = (data.get("url") or "").strip()
  start_sec = int(data.get("start_sec", 0))
  duration = max(5, min(120, int(data.get("duration", 25))))
  quality = normalize_quality(data.get("quality", "1080"))
  if not url:
    return jsonify({"error": "Нет исходной ссылки для ререндера"}), 400

  try:
    src_video = get_cached_raw_video(url, quality=quality)
    probe = subprocess.check_output([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        src_video,
    ])
    total_dur = max(float(duration) + 1.0, float(probe.decode().strip()))
    rid = str(uuid.uuid4())[:6]
    out_name = f"Kl1niClips_{quality}p_re_{rid}.mp4"
    out_path = os.path.join(OUTPUT_DIR, out_name)

    render_single_segment(
        src_video, out_path, start_sec, duration, data, total_dur=total_dur
    )
    file_url = f"{get_base_url()}/files/{out_name}"
    return jsonify({
        "filename": out_name,
        "quality": f"{quality}p",
        "url": file_url,
        "videoUrl": file_url,
    })
  except Exception as e:
    return jsonify({"error": f"Ошибка ререндера: {e}"}), 500


@app.route("/api/clips/<filename>", methods=["DELETE"])
def delete_clip_file(filename):
  safe_name = os.path.basename(filename)
  fpath = os.path.join(OUTPUT_DIR, safe_name)
  if not os.path.exists(fpath):
    return jsonify({"error": "Файл не найден"}), 404
  os.remove(fpath)
  return jsonify({"deleted": safe_name})


@app.route("/api/download-zip", methods=["POST"])
def download_zip():
  data = request.json or {}
  names = data.get("filenames") or []
  if not isinstance(names, list) or not names:
    return jsonify({"error": "Нет файлов для архива"}), 400

  mem = io.BytesIO()
  added = 0
  with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as zf:
    for raw_name in names:
      safe_name = os.path.basename(str(raw_name))
      fpath = os.path.join(OUTPUT_DIR, safe_name)
      if os.path.exists(fpath) and os.path.isfile(fpath):
        zf.write(fpath, arcname=safe_name)
        added += 1

  if added == 0:
    return jsonify({"error": "Файлы не найдены на диске"}), 404

  mem.seek(0)
  return send_file(
      mem,
      mimetype="application/zip",
      as_attachment=True,
      download_name="Kl1niClips_Batch.zip",
  )


@app.route("/api/preview", methods=["POST"])
def get_preview():
  data = request.json or {}
  url = (data.get("url") or "").strip()
  quality = normalize_quality(data.get("quality", "720"))
  if not url:
    return jsonify({"error": "Не указана ссылка на видео"}), 400

  try:
    title, _ = get_video_meta(url)
    src_video = get_cached_raw_video(url, quality=quality)
    fname = os.path.basename(src_video)
    local_stream = f"{get_base_url()}/cache/{fname}"
    return jsonify(
        {"title": title, "stream_url": local_stream, "quality": f"{quality}p"}
    )
  except subprocess.CalledProcessError as e:
    return (
        jsonify({"error": f"yt-dlp не смог загрузить видео (код {e.returncode})"}),
        500,
    )
  except Exception as e:
    return jsonify({"error": f"Ошибка предпросмотра: {e}"}), 500


@app.route("/cache/<path:filename>")
def serve_cached_file(filename):
  return send_from_directory(CACHE_DIR, filename, mimetype="video/mp4")


@app.route("/api/clips", methods=["GET"])
def list_existing_clips():
  base_url = get_base_url()
  files = [
      f
      for f in os.listdir(OUTPUT_DIR)
      if f.startswith("Kl1niClips_") and f.endswith(".mp4")
  ]
  files.sort(
      key=lambda name: os.path.getmtime(os.path.join(OUTPUT_DIR, name)),
      reverse=True,
  )
  result = []
  for idx, fname in enumerate(files[:40]):
    url = f"{base_url}/files/{fname}"
    q_match = re.search(r"_(1080|720|480)p_", fname)
    q_str = f"{q_match.group(1)}p" if q_match else "MP4"
    title_clean = fname.replace(".mp4", "")
    result.append({
        "id": fname,
        "filename": fname,
        "title": title_clean,
        "title_variants": [
            title_clean,
            f"Смотри до конца • {title_clean}",
            f"Топ момент • {title_clean}",
        ],
        "hashtags": "#tiktok #reels #shorts #гейминг #рек",
        "score": max(85, 97 - idx),
        "estimated_reach": f"{150 + (idx % 6) * 45}K – {420 + (idx % 6) * 90}K охватов",
        "ratio": "9:16",
        "quality": q_str,
        "url": url,
        "videoUrl": url,
        "isRealVideo": True,
    })
  return jsonify({"clips": result})


@app.route("/api/status/<job_id>", methods=["GET"])
def get_job_status(job_id):
  with JOBS_LOCK:
    job = JOBS.get(job_id)
  if not job:
    return jsonify({"error": "Задача не найдена"}), 404
  return jsonify(job)


def run_process_job(job_id: str, data: dict, base_url: str):
  try:
    url = (data.get("url") or "").strip()
    niche = (data.get("niche") or "gaming").strip().lower()
    cut_strategy = (data.get("cut_strategy") or "hype").strip().lower()
    quality = normalize_quality(data.get("quality", "720"))

    count = max(1, min(15, int(data.get("count", 5))))
    duration = max(5, min(120, int(data.get("duration", 25))))
    ratio = data.get("aspect_ratio", "9:16")

    range_start = data.get("range_start")
    range_end = data.get("range_end")

    set_job_status(
        job_id,
        "1/3 • Исходник и метаданные",
        f"Проверяем кэш и загружаем видео ({quality}p)...",
    )
    source_title, yt_tags = get_video_meta(url)
    src_video = get_cached_raw_video(url, quality=quality)

    probe = subprocess.check_output([
        "ffprobe",
        "-v",
        "error",
        "-show_entries",
        "format=duration",
        "-of",
        "default=noprint_wrappers=1:nokey=1",
        src_video,
    ])
    total_dur = max(float(duration) + 1.0, float(probe.decode().strip()))

    if cut_strategy == "series":
      set_job_status(
          job_id,
          "2/3 • Разбивка на серии",
          "Формируем последовательные части (Часть 1, 2, 3...)...",
          source_title=source_title,
      )
      hype_spots = find_series_timestamps(
          total_dur, count, duration, range_start=range_start, range_end=range_end
      )
    elif cut_strategy == "around":
      anchor = range_start if range_start is not None else 30
      set_job_status(
          job_id,
          "2/3 • Разброс вокруг таймкода",
          f"Собираем варианты захода вокруг {int(anchor)} сек...",
          source_title=source_title,
      )
      hype_spots = find_around_timestamps(total_dur, count, duration, anchor)
    else:
      set_job_status(
          job_id,
          "2/3 • Анализ пиков",
          "Анализируем громкость звука и выбираем лучшие моменты...",
          source_title=source_title,
      )
      hype_spots = find_hype_timestamps(
          src_video,
          total_dur,
          count,
          duration,
          range_start=range_start,
          range_end=range_end,
      )

    clips = []
    enc = detect_hw_encoder()

    for i, (start_sec, peak_db) in enumerate(hype_spots):
      set_job_status(
          job_id,
          f"3/3 • Рендер клипа {i + 1} из {len(hype_spots)} ({enc})",
          f"Собираем {quality}p без вотермарок (старт с {start_sec} сек)...",
          source_title=source_title,
      )
      out_name = f"Kl1niClips_{quality}p_{job_id}_{i+1}.mp4"
      out_path = os.path.join(OUTPUT_DIR, out_name)

      seg_params = dict(data)
      if (
          cut_strategy == "series"
          and not (seg_params.get("hook_text") or "").strip()
      ):
        seg_params["hook_text"] = f"ЧАСТЬ {i + 1}"

      render_single_segment(
          src_video,
          out_path,
          start_sec,
          duration,
          seg_params,
          total_dur=total_dur,
      )

      clip_title, title_variants, clip_hashtags = build_topic_and_hashtags(
          source_title,
          i,
          start_sec,
          peak_db,
          yt_tags,
          ratio,
          niche=niche,
          cut_strategy=cut_strategy,
      )
      score = max(86, 98 - i)
      reach_min = 140 + (score - 80) * 20
      reach_max = reach_min * 2 + 120
      file_url = f"{base_url}/files/{out_name}"
      clips.append({
          "id": f"{job_id}_{i+1}",
          "filename": out_name,
          "source_url": url,
          "start_sec": start_sec,
          "duration": duration,
          "title": clip_title,
          "title_variants": title_variants,
          "hashtags": clip_hashtags,
          "score": score,
          "estimated_reach": f"{reach_min}K – {reach_max}K охватов",
          "ratio": ratio,
          "quality": f"{quality}p",
          "url": file_url,
          "videoUrl": file_url,
          "isRealVideo": True,
      })

    set_job_status(
        job_id,
        "Готово!",
        f"Нарезано клипов: {len(clips)} ({quality}p, {enc}, без вотермарок).",
        done=True,
        clips=clips,
        source_title=source_title,
    )
  except Exception as e:
    set_job_status(job_id, "Ошибка", str(e), done=True, error=str(e))


@app.route("/process", methods=["POST"])
def process_video():
  data = request.json or {}
  url = (data.get("url") or "").strip()
  if not url:
    return jsonify({"error": "Не указана ссылка на видео"}), 400

  job_id = str(uuid.uuid4())[:8]
  base_url = get_base_url()
  set_job_status(
      job_id, "Старт задачи", "Поставили видео в очередь обработки..."
  )
  th = threading.Thread(
      target=run_process_job, args=(job_id, data, base_url), daemon=True
  )
  th.start()
  return jsonify({"job_id": job_id})


@app.route("/files/<path:filename>")
def serve_file(filename):
  force_dl = request.args.get("download") == "1"
  return send_from_directory(
      OUTPUT_DIR,
      filename,
      mimetype="video/mp4",
      as_attachment=force_dl,
      download_name=filename if force_dl else None,
  )


if __name__ == "__main__":
  lan_ip = get_lan_ip()
  enc_name = detect_hw_encoder()
  print(f"Kl1ni Clips запущен: http://127.0.0.1:5000 (кодер: {enc_name})")
  if lan_ip != "127.0.0.1":
    print(f"Доступ по локальной сети для телефона: http://{lan_ip}:5000")
  app.run(host="0.0.0.0", port=5000)
"""Рендер видео: эмуляция чата в мессенджере на экране смартфона, синхронно с аудио.

Кадры рисуются Pillow и отправляются потоком в ffmpeg (из imageio-ffmpeg),
который кодирует H.264 + AAC в MP4. Одинаковые подряд кадры не перерисовываются.
"""
import math
import os
import subprocess
import tempfile
import time

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

from audio_utils import NO_WINDOW, ffmpeg_exe, read_wav

# Макет задаётся в «dp» для экрана шириной 360 dp и масштабируется под разрешение видео
BASE_W, BASE_H = 360, 640
STATUS_H, HEADER_H, PLAYER_H = 24, 56, 64
SS = 3  # суперсэмплинг для гладких фигур

THEMES = {
    "light": {
        "status_bg": (255, 255, 255), "status_fg": (20, 20, 24),
        "header_bg": (255, 255, 255), "header_fg": (20, 20, 24), "header_sub": (128, 134, 145),
        "header_icon": (51, 144, 236), "header_speaking": (51, 144, 236),
        "divider": (226, 229, 234),
        "chat_bg_top": (224, 232, 241), "chat_bg_bottom": (209, 220, 233),
        "robot_bubble": (255, 255, 255), "robot_fg": (22, 24, 29), "robot_time": (150, 156, 166),
        "client_bubble": (51, 144, 236), "client_fg": (255, 255, 255), "client_time": (205, 228, 252),
        "accent": (51, 144, 236), "accent2": (102, 117, 245),
        "chip_bg": (0, 0, 0, 60), "chip_fg": (255, 255, 255),
        "player_bg": (255, 255, 255), "wave_off": (196, 205, 216), "player_fg": (128, 134, 145),
        "shadow": 38,
    },
    "dark": {
        "status_bg": (23, 33, 43), "status_fg": (240, 242, 245),
        "header_bg": (23, 33, 43), "header_fg": (240, 242, 245), "header_sub": (125, 139, 153),
        "header_icon": (240, 242, 245), "header_speaking": (106, 178, 242),
        "divider": (12, 18, 24),
        "chat_bg_top": (14, 22, 33), "chat_bg_bottom": (19, 30, 44),
        "robot_bubble": (24, 37, 51), "robot_fg": (240, 242, 245), "robot_time": (110, 127, 142),
        "client_bubble": (43, 82, 120), "client_fg": (240, 242, 245), "client_time": (125, 168, 211),
        "accent": (82, 136, 193), "accent2": (110, 102, 214),
        "chip_bg": (255, 255, 255, 30), "chip_fg": (220, 226, 232),
        "player_bg": (23, 33, 43), "wave_off": (52, 68, 84), "player_fg": (125, 139, 153),
        "shadow": 0,
    },
    "green": {
        "status_bg": (0, 128, 105), "status_fg": (255, 255, 255),
        "header_bg": (0, 128, 105), "header_fg": (255, 255, 255), "header_sub": (204, 236, 229),
        "header_icon": (255, 255, 255), "header_speaking": (255, 255, 255),
        "divider": (0, 110, 90),
        "chat_bg_top": (236, 229, 221), "chat_bg_bottom": (228, 220, 210),
        "robot_bubble": (255, 255, 255), "robot_fg": (22, 24, 29), "robot_time": (150, 150, 150),
        "client_bubble": (220, 248, 198), "client_fg": (22, 24, 29), "client_time": (120, 150, 110),
        "accent": (0, 150, 120), "accent2": (37, 211, 102),
        "chip_bg": (225, 243, 251, 255), "chip_fg": (80, 90, 100),
        "player_bg": (240, 240, 240), "wave_off": (200, 200, 200), "player_fg": (120, 120, 120),
        "shadow": 30,
    },
}

FONT_CANDIDATES = {
    "regular": ["segoeui.ttf", "arial.ttf", "DejaVuSans.ttf", "LiberationSans-Regular.ttf"],
    "semibold": ["seguisb.ttf", "segoeuib.ttf", "arialbd.ttf", "DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf"],
}
FONT_DIRS = [
    os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"),
    os.path.join(os.environ.get("LOCALAPPDATA", ""), "Microsoft", "Windows", "Fonts"),
    "/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/truetype/liberation", "/Library/Fonts",
]


def _font_path(kind):
    for name in FONT_CANDIDATES[kind]:
        for d in FONT_DIRS:
            p = os.path.join(d, name)
            if os.path.isfile(p):
                return p
    raise RuntimeError("Не найден системный шрифт с поддержкой кириллицы (Segoe UI / Arial)")


def fmt_time(sec):
    sec = max(0, int(sec))
    return "%d:%02d" % (sec // 60, sec % 60)


def ease_out(p):
    return 1 - (1 - p) ** 3


class ChatRenderer:
    def __init__(self, segments, duration, audio_wav, settings):
        self.W = int(settings.get("width", 1080))
        self.H = int(settings.get("height", 1920))
        self.fps = int(settings.get("fps", 30))
        self.S = self.W / BASE_W
        self.theme = THEMES.get(settings.get("theme"), THEMES["light"])
        self.robot_name = (settings.get("robot_name") or "Голосовой ассистент").strip()
        self.reveal_words = settings.get("reveal", "words") == "words"
        self.show_times = bool(settings.get("show_times", True))
        self.clock = (settings.get("clock") or time.strftime("%H:%M")).strip()
        self.duration = float(duration)

        self.segments = sorted(
            [dict(s) for s in segments if str(s.get("text", "")).strip()],
            key=lambda s: float(s["start"]))
        for s in self.segments:
            s["start"] = max(0.0, float(s["start"]))
            s["end"] = max(s["start"] + 0.2, float(s["end"]))
            s["words"] = s["text"].split()
            s["reveal_at"] = self._word_times(s)

        self.f_text = self._font("regular", 15)
        self.f_time = self._font("regular", 11)
        self.f_name = self._font("semibold", 16)
        self.f_sub = self._font("regular", 13)
        self.f_status = self._font("semibold", 13)
        self.f_chip = self._font("semibold", 12.5)

        self.chat_top = self.px(STATUS_H + HEADER_H)
        self.chat_h = self.H - self.chat_top - self.px(PLAYER_H)
        self._bubble_cache = {}
        self._header_cache = {}
        self._player_cache = {}
        self._chat_bg = self._gradient(self.W, self.chat_h, self.theme["chat_bg_top"], self.theme["chat_bg_bottom"])
        self._status = self._render_status()
        self._chip = self._render_chip("Сегодня")
        self._wave = self._waveform(audio_wav)
        self._scroll = 0.0

    # ------------------------------------------------------------------ утилиты
    def px(self, dp):
        return int(round(dp * self.S))

    def _font(self, kind, size_dp, scale=1):
        return ImageFont.truetype(_font_path(kind), max(6, int(round(size_dp * self.S * scale))))

    @staticmethod
    def _gradient(w, h, top, bottom):
        col = np.linspace(0, 1, h, dtype=np.float32)[:, None]
        arr = np.array(top, np.float32)[None, :] * (1 - col) + np.array(bottom, np.float32)[None, :] * col
        arr = np.repeat(arr[:, None, :], w, axis=1)
        return Image.fromarray(arr.astype(np.uint8), "RGB")

    def _word_times(self, s):
        """Момент появления каждого слова: равномерно по числу символов внутри реплики."""
        words = s["words"]
        total = sum(len(w) + 1 for w in words) or 1
        dur = s["end"] - s["start"]
        out, acc = [], 0
        for w in words:
            out.append(s["start"] + dur * acc / total)
            acc += len(w) + 1
        return out

    def _wrap(self, text, font, max_w):
        lines, cur = [], ""
        for word in text.split():
            cand = word if not cur else cur + " " + word
            if font.getlength(cand) <= max_w:
                cur = cand
                continue
            if cur:
                lines.append(cur)
            # слово длиннее строки — режем посимвольно
            while font.getlength(word) > max_w:
                cut = len(word)
                while cut > 1 and font.getlength(word[:cut]) > max_w:
                    cut -= 1
                lines.append(word[:cut])
                word = word[cut:]
            cur = word
        if cur:
            lines.append(cur)
        return lines or [""]

    def _rrect_mask(self, w, h, r, small_corner=None, r_small=0):
        """Маска скруглённого прямоугольника со сглаживанием; один угол может быть «хвостиком»."""
        big = Image.new("L", (w * SS, h * SS), 0)
        d = ImageDraw.Draw(big)
        R = r * SS
        corners = [True, True, True, True]  # tl, tr, br, bl
        if small_corner is not None:
            corners[small_corner] = False
        d.rounded_rectangle([0, 0, w * SS - 1, h * SS - 1], radius=R, fill=255, corners=tuple(corners))
        if small_corner is not None:
            x0 = 0 if small_corner in (0, 3) else w * SS - R
            y0 = 0 if small_corner in (0, 1) else h * SS - R
            d.rectangle([x0, y0, x0 + R - 1, y0 + R - 1], fill=0)
            sc = [False] * 4
            sc[small_corner] = True
            d.rounded_rectangle([x0, y0, x0 + R - 1, y0 + R - 1], radius=max(1, r_small * SS), fill=255,
                                corners=tuple(sc))
        return big.resize((w, h), Image.LANCZOS)

    # ------------------------------------------------------------------ элементы
    def _render_status(self):
        W, h = self.W, self.px(STATUS_H)
        th = self.theme
        img = Image.new("RGB", (W * SS, h * SS), th["status_bg"])
        d = ImageDraw.Draw(img)
        s = self.S * SS
        fg = th["status_fg"]
        font = self._font("semibold", 13, SS)
        d.text((20 * s, 12.5 * s), self.clock, font=font, fill=fg, anchor="lm")
        # батарея
        bx, by, bw, bh = (360 - 16 - 24) * s, 7 * s, 22 * s, 11 * s
        d.rounded_rectangle([bx, by, bx + bw, by + bh], radius=3 * s, outline=fg, width=max(1, int(1.2 * s)))
        d.rounded_rectangle([bx + bw + 1 * s, by + 3.5 * s, bx + bw + 2.6 * s, by + bh - 3.5 * s], radius=1 * s, fill=fg)
        d.rounded_rectangle([bx + 2.2 * s, by + 2.2 * s, bx + bw * 0.78, by + bh - 2.2 * s], radius=1.5 * s, fill=fg)
        # wi-fi: три дуги и точка
        cx, cy = (360 - 16 - 24 - 16) * s, 17.5 * s
        for i, rad in enumerate((10.5, 7, 3.5)):
            r = rad * s
            if i < 2:
                d.arc([cx - r, cy - r, cx + r, cy + r], 225, 315, fill=fg, width=int(2 * s))
            else:
                d.pieslice([cx - r, cy - r, cx + r, cy + r], 225, 315, fill=fg)
        # сеть: четыре столбика
        sx = (360 - 16 - 24 - 16 - 14 - 18) * s
        for i in range(4):
            bh2 = (4 + i * 2.4) * s
            d.rounded_rectangle([sx + i * 4.4 * s, 18 * s - bh2, sx + i * 4.4 * s + 3 * s, 18 * s],
                                radius=0.8 * s, fill=fg)
        return img.resize((W, h), Image.LANCZOS)

    def _render_avatar(self, size):
        th = self.theme
        n = size * SS
        grad = self._gradient(n, n, th["accent2"], th["accent"])
        mask = Image.new("L", (n, n), 0)
        ImageDraw.Draw(mask).ellipse([0, 0, n - 1, n - 1], fill=255)
        img = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        img.paste(grad, (0, 0), mask)
        d = ImageDraw.Draw(img)
        u = n / 40.0
        white = (255, 255, 255, 255)
        # антенна
        d.line([20 * u, 8 * u, 20 * u, 13 * u], fill=white, width=int(2 * u))
        d.ellipse([17.5 * u, 6 * u, 22.5 * u, 11 * u], fill=white)
        # голова и «уши»
        d.rounded_rectangle([10 * u, 13 * u, 30 * u, 30 * u], radius=5 * u, fill=white)
        d.rounded_rectangle([7 * u, 18 * u, 10 * u, 25 * u], radius=1.2 * u, fill=white)
        d.rounded_rectangle([30 * u, 18 * u, 33 * u, 25 * u], radius=1.2 * u, fill=white)
        # глаза и рот
        eye = th["accent"] + (255,)
        d.ellipse([14 * u, 18 * u, 18.5 * u, 22.5 * u], fill=eye)
        d.ellipse([21.5 * u, 18 * u, 26 * u, 22.5 * u], fill=eye)
        d.rounded_rectangle([16 * u, 25 * u, 24 * u, 26.8 * u], radius=0.9 * u, fill=eye)
        return img.resize((size, size), Image.LANCZOS)

    def _header(self, speaking):
        if speaking in self._header_cache:
            return self._header_cache[speaking]
        th = self.theme
        W, h = self.W, self.px(HEADER_H)
        img = Image.new("RGB", (W, h), th["header_bg"])
        big = Image.new("RGBA", (self.px(30) * SS, h * SS), th["header_bg"] + (255,))
        d = ImageDraw.Draw(big)
        s = self.S * SS
        # стрелка «назад»
        d.line([(19 * s, 19 * s), (11 * s, 28 * s), (19 * s, 37 * s)], fill=th["header_icon"],
               width=int(2.4 * s), joint="curve")
        img.paste(big.resize((self.px(30), h), Image.LANCZOS).convert("RGB"), (0, 0))
        av = self._render_avatar(self.px(40))
        img.paste(av, (self.px(36), self.px(8)), av)
        d = ImageDraw.Draw(img)
        name = self.robot_name
        max_w = self.px(360 - 86 - 40)
        while self.f_name.getlength(name) > max_w and len(name) > 1:
            name = name[:-2] + "…"
        d.text((self.px(86), self.px(19)), name, font=self.f_name, fill=th["header_fg"], anchor="lm")
        sub, color = ("говорит…", th["header_speaking"]) if speaking else ("в сети", th["header_sub"])
        d.text((self.px(86), self.px(38)), sub, font=self.f_sub, fill=color, anchor="lm")
        # «три точки» справа
        for i in range(3):
            cy = self.px(20 + i * 7.5)
            r = self.px(1.9)
            cx = self.W - self.px(20)
            d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=th["header_fg"])
        d.line([0, h - 1, W, h - 1], fill=th["divider"], width=max(1, self.px(0.5)))
        self._header_cache[speaking] = img
        return img

    def _render_chip(self, text):
        th = self.theme
        tw = int(self.f_chip.getlength(text))
        w, h = tw + self.px(20), self.px(24)
        mask = self._rrect_mask(w, h, h // 2)
        bg = th["chip_bg"]
        img = Image.new("RGBA", (w, h), bg[:3] + (0,))
        fill = Image.new("RGBA", (w, h), bg[:3] + (255,))
        alpha = mask.point(lambda v: v * (bg[3] if len(bg) > 3 else 255) // 255)
        img.paste(fill, (0, 0), mask)
        img.putalpha(alpha)
        ImageDraw.Draw(img).text((w / 2, h / 2), text, font=self.f_chip, fill=th["chip_fg"], anchor="mm")
        return img

    def _bubble(self, idx, n_words):
        key = (idx, n_words)
        if key in self._bubble_cache:
            return self._bubble_cache[key]
        seg = self.segments[idx]
        th = self.theme
        robot = seg["speaker"] == "robot"
        text = " ".join(seg["words"][:n_words])
        pad_x, pad_y, line_h = self.px(12), self.px(7), self.px(20)
        max_text_w = self.px(360 * 0.78) - 2 * pad_x
        lines = self._wrap(text, self.f_text, max_text_w)
        widths = [self.f_text.getlength(l) for l in lines]
        time_txt = fmt_time(seg["start"]) if self.show_times else ""
        time_w = self.f_time.getlength(time_txt) if time_txt else 0
        gap = self.px(8)
        content_w = max(widths)
        content_h = len(lines) * line_h
        if time_txt:
            if widths[-1] + gap + time_w <= max_text_w:
                content_w = max(content_w, widths[-1] + gap + time_w)
            else:
                content_w = max(content_w, time_w)
                content_h += self.px(14)
        bw = int(math.ceil(content_w)) + 2 * pad_x
        bh = content_h + 2 * pad_y
        m = self.px(3)  # поле под тень
        img = Image.new("RGBA", (bw + 2 * m, bh + 2 * m), (0, 0, 0, 0))
        mask = self._rrect_mask(bw, bh, self.px(16), 3 if robot else 2, self.px(4))
        if th["shadow"]:
            sh = Image.new("L", img.size, 0)
            sh.paste(mask.point(lambda v: v * th["shadow"] // 255), (m, m + self.px(1)))
            sh = sh.filter(ImageFilter.GaussianBlur(self.px(1.2)))
            img.putalpha(sh)
        color = th["robot_bubble"] if robot else th["client_bubble"]
        img.paste(Image.new("RGBA", (bw, bh), color + (255,)), (m, m), mask)
        d = ImageDraw.Draw(img)
        fg = th["robot_fg"] if robot else th["client_fg"]
        baseline = self.px(14.6)
        for i, line in enumerate(lines):
            d.text((m + pad_x, m + pad_y + i * line_h + baseline), line, font=self.f_text, fill=fg, anchor="ls")
        if time_txt:
            d.text((m + bw - pad_x, m + bh - pad_y - self.px(1)), time_txt, font=self.f_time,
                   fill=th["robot_time"] if robot else th["client_time"], anchor="rs")
        res = (img, bw, bh, m)
        self._bubble_cache[key] = res
        # для каждой реплики нужна только последняя версия — старые выбрасываем
        old = (idx, n_words - 1)
        self._bubble_cache.pop(old, None)
        return res

    def _waveform(self, wav_path, bars=58):
        try:
            data, _ = read_wav(wav_path)
            x = np.abs(data).mean(axis=0)
        except Exception:
            return [0.3] * bars
        chunks = np.array_split(x, bars)
        env = np.array([np.sqrt(np.mean(c ** 2)) if len(c) else 0.0 for c in chunks])
        if env.max() > 0:
            env = env / env.max()
        return list(np.clip(env ** 0.6, 0.08, 1.0))

    def _player(self, t):
        bars = len(self._wave)
        played = int(bars * min(1.0, t / max(self.duration, 0.01)))
        key = (played, int(t))
        if key in self._player_cache:
            return self._player_cache[key]
        th = self.theme
        W, h = self.W, self.px(PLAYER_H)
        big = Image.new("RGB", (W * SS, h * SS), th["player_bg"])
        d = ImageDraw.Draw(big)
        s = self.S * SS
        d.line([0, 0, W * SS, 0], fill=th["divider"], width=max(1, int(0.6 * s)))
        # кнопка «пауза» (запись воспроизводится)
        cx, cy, r = 34 * s, 32 * s, 20 * s
        d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=th["accent"])
        for dx in (-4.5, 2):
            d.rounded_rectangle([cx + dx * s, cy - 7 * s, cx + (dx + 2.6) * s, cy + 7 * s], radius=1 * s,
                                fill=(255, 255, 255))
        # «волна» записи
        x0, x1 = 66 * s, (360 - 16) * s
        step = (x1 - x0) / bars
        bw = step * 0.55
        for i, a in enumerate(self._wave):
            bh = (3 + a * 19) * s
            x = x0 + i * step
            col = th["accent"] if i < played else th["wave_off"]
            d.rounded_rectangle([x, 26 * s - bh / 2, x + bw, 26 * s + bh / 2], radius=bw / 2, fill=col)
        tf = self._font("regular", 11.5, SS)
        d.text((x0, 50 * s), fmt_time(t), font=tf, fill=th["player_fg"], anchor="lm")
        d.text((x1, 50 * s), fmt_time(self.duration), font=tf, fill=th["player_fg"], anchor="rm")
        img = big.resize((W, h), Image.LANCZOS)
        self._player_cache = {key: img}
        return img

    # ------------------------------------------------------------------ кадр
    def _words_shown(self, seg, t):
        if not self.reveal_words:
            return len(seg["words"])
        n = 0
        for rt in seg["reveal_at"]:
            if rt <= t + 1e-6:
                n += 1
        return max(1, n)

    def _layout(self, t):
        items = []
        y = self.px(10)
        items.append(("chip", y))
        y += self._chip.size[1] + self.px(8)
        prev = None
        speaking = False
        for i, seg in enumerate(self.segments):
            if seg["start"] > t:
                break
            if seg["speaker"] == "robot" and seg["start"] <= t <= seg["end"]:
                speaking = True
            if prev is not None:
                y += self.px(3) if prev == seg["speaker"] else self.px(10)
            n = self._words_shown(seg, t)
            img, bw, bh, m = self._bubble(i, n)
            p = ease_out(min(1.0, (t - seg["start"]) / 0.25))
            items.append(("bubble", y, i, n, p))
            y += bh
            prev = seg["speaker"]
        return items, y + self.px(12), speaking

    def _draw_chat(self, items, scroll):
        chat = self._chat_bg.copy()
        for it in items:
            if it[0] == "chip":
                y = it[1] - scroll
                if -self._chip.size[1] < y < self.chat_h:
                    chat.paste(self._chip, ((self.W - self._chip.size[0]) // 2, int(y)), self._chip)
                continue
            _, y, i, n, p = it
            img, bw, bh, m = self._bubble(i, n)
            y = y - scroll + (1 - p) * self.px(10)
            if y > self.chat_h or y + bh < 0:
                continue
            robot = self.segments[i]["speaker"] == "robot"
            x = self.px(8) if robot else self.W - self.px(8) - bw
            if p < 1:
                faded = img.copy()
                faded.putalpha(img.getchannel("A").point(lambda v: int(v * p)))
                img = faded
            chat.paste(img, (int(x - m), int(round(y - m))), img)
        return chat

    def frames(self):
        """Генератор (номер кадра, байты RGB24). Одинаковые кадры не перерисовываются."""
        total = int(math.ceil(self.duration * self.fps))
        frame = Image.new("RGB", (self.W, self.H))
        frame.paste(self._status, (0, 0))
        prev_key, prev_bytes = None, None
        k = 1 - math.exp(-(1.0 / self.fps) / 0.07)
        for fi in range(total):
            t = fi / self.fps
            items, content_h, speaking = self._layout(t)
            target = max(0.0, content_h - self.chat_h)
            self._scroll += (target - self._scroll) * k
            if abs(target - self._scroll) < 0.5:
                self._scroll = target
            scroll = int(round(self._scroll))
            bars = len(self._wave)
            pkey = (int(bars * min(1.0, t / max(self.duration, 0.01))), int(t))
            ckey = tuple((it[2], it[3], round(it[4], 3)) for it in items if it[0] == "bubble")
            key = (speaking, pkey, ckey, scroll)
            if key != prev_key:
                frame.paste(self._header(speaking), (0, self.px(STATUS_H)))
                frame.paste(self._draw_chat(items, scroll), (0, self.chat_top))
                frame.paste(self._player(t), (0, self.H - self.px(PLAYER_H)))
                prev_bytes = frame.tobytes()
                prev_key = key
            yield fi, total, prev_bytes

    def frame_at(self, t):
        """Один кадр (для предпросмотра) — прокрутка сразу в конечное положение."""
        items, content_h, speaking = self._layout(t)
        scroll = int(max(0.0, content_h - self.chat_h))
        frame = Image.new("RGB", (self.W, self.H))
        frame.paste(self._status, (0, 0))
        frame.paste(self._header(speaking), (0, self.px(STATUS_H)))
        frame.paste(self._draw_chat(items, scroll), (0, self.chat_top))
        frame.paste(self._player(t), (0, self.H - self.px(PLAYER_H)))
        return frame


def render_video(segments, duration, audio_wav, out_path, settings, progress_cb=None):
    r = ChatRenderer(segments, duration, audio_wav, settings)
    log = tempfile.TemporaryFile()
    cmd = [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y",
        "-f", "rawvideo", "-pix_fmt", "rgb24", "-s", "%dx%d" % (r.W, r.H), "-r", str(r.fps), "-i", "-",
        "-i", audio_wav,
        "-map", "0:v:0", "-map", "1:a:0",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
        "-c:a", "aac", "-b:a", "160k", "-ac", "2",
        "-shortest", "-movflags", "+faststart", out_path,
    ]
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=log,
                            creationflags=NO_WINDOW)
    try:
        for fi, total, data in r.frames():
            proc.stdin.write(data)
            if progress_cb and fi % 15 == 0:
                progress_cb(fi / max(total, 1))
        proc.stdin.close()
        code = proc.wait()
    except (BrokenPipeError, OSError):
        code = proc.wait()
    if code != 0:
        log.seek(0)
        err = log.read().decode("utf-8", errors="replace").strip()
        raise RuntimeError("Ошибка кодирования видео (ffmpeg): " + err[-500:])
    log.close()
    if progress_cb:
        progress_cb(1.0)

"""Простое разделение реплик по говорящим без нейросетевых моделей.

Используется, когда запись моно. Для каждой реплики считаются MFCC-признаки
(тембр голоса и канала связи), затем реплики делятся на два кластера k-means.
Голос робота (синтез речи) обычно сильно отличается от голоса клиента
в телефонном канале, поэтому такого подхода, как правило, достаточно.
Ошибки легко поправить в редакторе вручную.
"""
import numpy as np

SR = 16000
N_FFT = 512
HOP = 160
N_MELS = 40
N_MFCC = 20


def _mel_filterbank(sr=SR, n_fft=N_FFT, n_mels=N_MELS, fmin=60.0, fmax=7600.0):
    def hz_to_mel(f):
        return 2595.0 * np.log10(1.0 + f / 700.0)

    def mel_to_hz(m):
        return 700.0 * (10 ** (m / 2595.0) - 1.0)

    mels = np.linspace(hz_to_mel(fmin), hz_to_mel(fmax), n_mels + 2)
    hz = mel_to_hz(mels)
    bins = np.floor((n_fft + 1) * hz / sr).astype(int)
    fb = np.zeros((n_mels, n_fft // 2 + 1), dtype=np.float32)
    for i in range(1, n_mels + 1):
        left, center, right = bins[i - 1], bins[i], bins[i + 1]
        for k in range(left, center):
            if center > left:
                fb[i - 1, k] = (k - left) / (center - left)
        for k in range(center, right):
            if right > center:
                fb[i - 1, k] = (right - k) / (right - center)
    return fb


_FB = _mel_filterbank()
_DCT = np.cos(np.pi / N_MELS * (np.arange(N_MELS)[None, :] + 0.5) * np.arange(N_MFCC)[:, None]).astype(np.float32)
_WINDOW = np.hanning(N_FFT).astype(np.float32)


def _mfcc(x):
    if len(x) < N_FFT:
        x = np.pad(x, (0, N_FFT - len(x)))
    n_frames = 1 + (len(x) - N_FFT) // HOP
    idx = np.arange(N_FFT)[None, :] + HOP * np.arange(n_frames)[:, None]
    frames = x[idx] * _WINDOW
    power = np.abs(np.fft.rfft(frames, n=N_FFT)) ** 2
    logmel = np.log(power @ _FB.T + 1e-8)
    energy = logmel.mean(axis=1)
    mfcc = logmel @ _DCT.T
    return mfcc[:, 1:], energy  # c0 (громкость) не используем


def _kmeans2(X, weights, n_init=10, iters=50, seed=0):
    rng = np.random.default_rng(seed)
    best_labels, best_inertia = None, np.inf
    for _ in range(n_init):
        centers = X[rng.choice(len(X), 2, replace=False)]
        for _ in range(iters):
            d = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
            labels = d.argmin(axis=1)
            new_centers = centers.copy()
            for k in range(2):
                m = labels == k
                if m.any():
                    new_centers[k] = np.average(X[m], axis=0, weights=weights[m])
            if np.allclose(new_centers, centers):
                break
            centers = new_centers
        d = ((X[:, None, :] - centers[None, :, :]) ** 2).sum(axis=2)
        inertia = (d.min(axis=1) * weights).sum()
        if inertia < best_inertia:
            best_inertia, best_labels = inertia, d.argmin(axis=1)
    return best_labels


def _voiced_frames(audio, start, end):
    a = int(max(0.0, start) * SR)
    b = int(max(start + 0.05, end) * SR)
    x = audio[a:b]
    if len(x) < N_FFT:
        return None
    mfcc, energy = _mfcc(x)
    voiced = mfcc[energy >= np.percentile(energy, 40)]
    return voiced if len(voiced) >= 3 else mfcc


def assign_speakers(audio, segments, iterations=8):
    """Проставляет segment['speaker'] = 'robot' | 'client' для моно-записи.

    1. Длинные фразы (надёжный материал) делятся на 2 группы k-means по среднему тембру.
    2. По кадрам каждой группы строится модель голоса (гауссиана в пространстве MFCC).
    3. Каждая фраза, включая короткие, относится к голосу, который лучше объясняет её кадры;
       модели уточняются по новой разметке, пока она не перестанет меняться.
    """
    if not segments:
        return segments
    frames = [_voiced_frames(audio, s["start"], s["end"]) for s in segments]
    ok = [i for i, f in enumerate(frames) if f is not None]
    labels = np.zeros(len(segments), dtype=int)
    if len(ok) >= 2:
        allf = np.concatenate([frames[i] for i in ok])
        mu, sd = allf.mean(axis=0), allf.std(axis=0) + 1e-6
        norm = {i: (frames[i] - mu) / sd for i in ok}
        dur = {i: segments[i]["end"] - segments[i]["start"] for i in ok}

        # 1. начальное разбиение по длинным фразам
        seed = [i for i in ok if dur[i] >= 1.5]
        if len(seed) < 2:
            seed = ok
        X = np.array([np.concatenate([norm[i].mean(axis=0), norm[i].std(axis=0)]) for i in seed])
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-6)
        lab = _kmeans2(X, np.array([max(0.3, dur[i]) for i in seed]))
        cur = {i: int(l) for i, l in zip(seed, lab)}

        # 2–3. модели голосов и переразметка всех фраз
        for _ in range(iterations):
            models = []
            for k in range(2):
                fk = [norm[i] for i in cur if cur[i] == k]
                if not fk:
                    break
                fk = np.concatenate(fk)
                models.append((fk.mean(axis=0), fk.var(axis=0) + 0.05))
            if len(models) < 2:
                break
            new = {}
            for i in ok:
                ll = [(-0.5 * (((norm[i] - m) ** 2) / v + np.log(v)).sum(axis=1)).mean() for m, v in models]
                new[i] = int(np.argmax(ll))
            if new == cur:
                break
            cur = new
        for i in ok:
            labels[i] = cur.get(i, 0)
        # фразы без признаков (слишком короткие) — как у соседа слева
        ok_set = set(ok)
        for i in range(1, len(segments)):
            if i not in ok_set:
                labels[i] = labels[i - 1]
    for s, lab in zip(segments, labels):
        s["speaker"] = int(lab)
    return label_robot_by_talk_time(segments)


def trim_silence(audio, segments, pad=0.08):
    """Сдвигает границы фраз к реальному началу и концу речи.

    Whisper часто растягивает первое слово ответа на паузу перед ним — из-за этого
    бабл появлялся бы раньше, чем человек заговорил.
    """
    hop = int(0.02 * SR)
    n = len(audio) // hop
    if n == 0:
        return segments
    rms = np.sqrt((audio[: n * hop].reshape(n, hop) ** 2).mean(axis=1))
    floor = np.percentile(rms, 10)
    for s in segments:
        a, b = int(s["start"] / 0.02), min(n, int(np.ceil(s["end"] / 0.02)))
        if b - a < 3:
            continue
        seg = rms[a:b]
        thr = max(floor * 4, seg.max() * 0.12)
        idx = np.nonzero(seg > thr)[0]
        if len(idx) == 0:
            continue
        new_start = (a + idx[0]) * 0.02 - pad
        new_end = (a + idx[-1] + 1) * 0.02 + pad
        if new_end - new_start >= 0.2:
            s["start"] = max(s["start"], new_start)
            s["end"] = min(s["end"], new_end)
    return segments


def label_robot_by_talk_time(segments):
    """Переводит числовые метки 0/1 в robot/client.

    Робот, как правило, говорит длинными фразами и суммарно дольше клиента,
    поэтому роботом считается тот, у кого больше общее время речи.
    Если угадали неверно — в интерфейсе есть кнопка «Поменять местами».
    """
    total = {}
    for s in segments:
        total[s["speaker"]] = total.get(s["speaker"], 0.0) + (s["end"] - s["start"])
    robot = max(total, key=total.get) if total else 0
    for s in segments:
        s["speaker"] = "robot" if s["speaker"] == robot else "client"
    return segments

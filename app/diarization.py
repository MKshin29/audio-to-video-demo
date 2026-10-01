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


def segment_features(audio, start, end):
    """Вектор признаков одной реплики: среднее и разброс MFCC по озвученным кадрам."""
    a = int(max(0.0, start) * SR)
    b = int(max(start + 0.05, end) * SR)
    x = audio[a:b]
    if len(x) < N_FFT:
        return None
    mfcc, energy = _mfcc(x)
    # берём только громкие кадры — там действительно звучит голос
    thr = np.percentile(energy, 40)
    voiced = mfcc[energy >= thr]
    if len(voiced) < 3:
        voiced = mfcc
    return np.concatenate([voiced.mean(axis=0), voiced.std(axis=0)])


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


def assign_speakers(audio, segments):
    """Проставляет segment['speaker'] = 'robot' | 'client' для моно-записи."""
    if not segments:
        return segments
    feats, ok = [], []
    for i, s in enumerate(segments):
        f = segment_features(audio, s["start"], s["end"])
        if f is not None:
            feats.append(f)
            ok.append(i)
    labels = np.zeros(len(segments), dtype=int)
    if len(feats) >= 2:
        X = np.array(feats)
        X = (X - X.mean(axis=0)) / (X.std(axis=0) + 1e-6)
        w = np.array([max(0.3, segments[i]["end"] - segments[i]["start"]) for i in ok])
        lab = _kmeans2(X, w)
        for j, i in enumerate(ok):
            labels[i] = lab[j]
        # реплики без признаков (слишком короткие) — как у соседа слева
        ok_set = set(ok)
        for i in range(len(segments)):
            if i not in ok_set and i > 0:
                labels[i] = labels[i - 1]
    for s, lab in zip(segments, labels):
        s["speaker"] = int(lab)
    return label_robot_by_talk_time(segments)


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

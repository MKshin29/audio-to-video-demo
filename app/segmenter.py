"""Деление длинных фраз на несколько баблов по предложениям."""
import re

SENT_END = re.compile(r"[.!?…]+[\"»”)\]]*$")
CLAUSE_END = re.compile(r"[,;:]+[\"»”)\]]*$")
DASHES = {"—", "–", "-"}


def _tokens(seg):
    """Слова реплики с временем каждого: (текст, начало, конец).

    Если есть границы слов от распознавания и текст с ними совпадает — берём их,
    иначе (текст правили вручную) распределяем время пропорционально числу символов.
    """
    text = seg["text"].strip()
    words = seg.get("words") or []
    if words:
        joined = "".join(w["word"] for w in words)
        if re.sub(r"\s+", "", joined) == re.sub(r"\s+", "", text):
            return [(w["word"].strip(), float(w["start"]), float(w["end"])) for w in words if w["word"].strip()]
    parts = text.split()
    start, end = float(seg["start"]), float(seg["end"])
    total = sum(len(p) + 1 for p in parts) or 1
    out, acc = [], 0
    for p in parts:
        a = start + (end - start) * acc / total
        acc += len(p) + 1
        out.append((p, a, start + (end - start) * acc / total))
    return out


def _length(tokens):
    return sum(len(t[0]) for t in tokens) + max(0, len(tokens) - 1)


def _cut(tokens, is_boundary):
    """Режет список слов после каждого слова, на котором is_boundary(i) истинно."""
    out, cur = [], []
    for i, t in enumerate(tokens):
        cur.append(t)
        if is_boundary(i) and i < len(tokens) - 1:
            out.append(cur)
            cur = []
    if cur:
        out.append(cur)
    return out


def _units(tokens, max_chars):
    """Предложения; слишком длинные предложения — части по запятым, в крайнем случае по словам."""
    units = []
    for sent in _cut(tokens, lambda i: bool(SENT_END.search(tokens[i][0]))):
        if _length(sent) <= max_chars:
            units.append(sent)
            continue
        clauses = _cut(sent, lambda i: bool(CLAUSE_END.search(sent[i][0]))
                       or (i + 1 < len(sent) and sent[i + 1][0] in DASHES))
        for cl in clauses:
            if _length(cl) <= max_chars:
                units.append(cl)
            else:
                units.extend([[t] for t in cl])  # дальше склеятся по словам до лимита
    return units


def _group(units, max_chars):
    chunks, cur = [], []
    for u in units:
        if cur and _length(cur) + 1 + _length(u) > max_chars:
            chunks.append(cur)
            cur = []
        cur = cur + u
    if cur:
        chunks.append(cur)
    return chunks


def split_long(segments, max_chars=200):
    """Реплики длиннее max_chars делятся на баблы по несколько предложений (не длиннее max_chars)."""
    max_chars = int(max_chars or 0)
    if max_chars <= 0:
        return segments
    max_chars = max(40, max_chars)
    out = []
    for seg in segments:
        if len(seg["text"].strip()) <= max_chars:
            out.append(seg)
            continue
        tokens = _tokens(seg)
        if not tokens:
            out.append(seg)
            continue
        for chunk in _group(_units(tokens, max_chars), max_chars):
            piece = dict(seg)
            piece.pop("words", None)
            piece.update(text=" ".join(t[0] for t in chunk),
                         start=round(chunk[0][1], 2), end=round(chunk[-1][2], 2))
            out.append(piece)
    return out

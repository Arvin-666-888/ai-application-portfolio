class RecursiveTextSplitter:
    def __init__(self, chunk_size=400, chunk_overlap=80, separators=None):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or [
            "\n\n", "\n", "。", "！", "？", "；",
            "，", " ", ""
        ]

    def split_text(self, text: str) -> list[str]:
        if not text or not text.strip():
            return []
        if len(text) <= self.chunk_size:
            return [text.strip()]

        for sep in self.separators:
            if sep and sep in text:
                parts = text.split(sep)
                return self._merge_parts(parts, sep)

        return self._force_split(text)

    def _merge_parts(self, parts: list[str], sep: str) -> list[str]:
        chunks = []
        current = ""

        for part in parts:
            if not part.strip():
                continue
            candidate = current + sep + part if current else part
            if len(candidate) <= self.chunk_size:
                current = candidate
            else:
                if current:
                    chunks.append(current.strip())
                if len(part) > self.chunk_size:
                    sub_chunks = self._force_split(part)
                    chunks.extend(sub_chunks[:-1])
                    current = sub_chunks[-1] if sub_chunks else ""
                else:
                    current = part

        if current:
            chunks.append(current.strip())

        return self._add_overlap(chunks)

    def _add_overlap(self, chunks: list[str]) -> list[str]:
        if len(chunks) <= 1 or self.chunk_overlap == 0:
            return chunks

        result = [chunks[0]]
        for i in range(1, len(chunks)):
            prev = chunks[i - 1]
            overlap_text = prev[-self.chunk_overlap:] if len(prev) > self.chunk_overlap else prev
            result.append(overlap_text + chunks[i])

        return result

    def _force_split(self, text: str) -> list[str]:
        chunks = []
        for i in range(0, len(text), self.chunk_size):
            chunk = text[i:i + self.chunk_size].strip()
            if chunk:
                chunks.append(chunk)
        return chunks

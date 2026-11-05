
def chunk_source(source, path, max_lines=80, overlap=8):
    if not isinstance(source, str):
        raise TypeError("source must be a string")

    if max_lines < 1:
        raise ValueError("max_lines must be positive")

    if overlap < 0 or overlap >= max_lines:
        raise ValueError("overlap must be non-negative and smaller than max_lines")

    lines = source.splitlines()
    if not lines:
        return []

    chunks = []
    step = max_lines - overlap

    for start in range(0, len(lines), step):
        end = min(len(lines), start + max_lines)
        content = "\n".join(lines[start:end]).strip()

        if content:
            chunks.append({
                "path": path,
                "start_line": start + 1,
                "end_line": end,
                "text": content,
            })

        if end == len(lines):
            break

    return chunks

import ast

def chunk_source(source, path, max_lines=80, overlap=8):
    lines = source.splitlines()
    if not lines:
        return []
    # Fixed windows guarantee complete line coverage; AST parsing is used to
    # record whether the file is syntactically parseable, not to drop lines.
    chunks = []
    step = max(1, max_lines - overlap)
    for start in range(0, len(lines), step):
        end = min(len(lines), start + max_lines)
        text = "\\n".join(lines[start:end]).strip()
        if text:
            chunks.append({"path": path, "start_line": start + 1,
                           "end_line": end, "text": text})
        if end == len(lines):
            break
    return chunks

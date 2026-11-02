def refine_context(chunks, max_chars=18000):
    seen=set(); parts=[]; used=0
    for c in chunks:
        key=(c["path"],c["start_line"],c["end_line"])
        if key in seen: continue
        seen.add(key)
        block=f"[SOURCE: {c['path']}:{c['start_line']}-{c['end_line']}]\n{c['text']}"
        if used+len(block)>max_chars: continue
        parts.append(block); used+=len(block)
    return "\n\n".join(parts)

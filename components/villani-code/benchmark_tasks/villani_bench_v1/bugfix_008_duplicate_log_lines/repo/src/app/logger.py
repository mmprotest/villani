def dedupe_lines(lines: list[str]) -> list[str]:
    seen = set()
    out = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        out.append(line)
    return sorted(out)

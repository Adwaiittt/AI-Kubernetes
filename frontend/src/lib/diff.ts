export type DiffLine = { kind: "add" | "del" | "ctx" | "hunk" | "meta"; text: string };

/** Classify the lines of a unified diff for display. */
export function parseDiff(diff: string): DiffLine[] {
  return diff
    .split("\n")
    .filter((line, i, all) => !(line === "" && i === all.length - 1))
    .map((line): DiffLine => {
      if (line.startsWith("+++") || line.startsWith("---")) return { kind: "meta", text: line };
      if (line.startsWith("@@")) return { kind: "hunk", text: line };
      if (line.startsWith("+")) return { kind: "add", text: line };
      if (line.startsWith("-")) return { kind: "del", text: line };
      return { kind: "ctx", text: line };
    });
}

export function diffStats(lines: DiffLine[]): { added: number; removed: number } {
  return {
    added: lines.filter((l) => l.kind === "add").length,
    removed: lines.filter((l) => l.kind === "del").length,
  };
}

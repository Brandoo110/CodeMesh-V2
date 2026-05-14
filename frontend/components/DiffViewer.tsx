"use client";

/**
 * Side-by-side file diff 渲染器（v5 Phase 6.6 — 护城河 #3）。
 *
 * 自实现行级 diff，不引外部包（react-diff-viewer-continued 等）。理由：
 *   - 后端传完整 before / after 字符串，行级比较足够清晰
 *   - 避免 npm 依赖膨胀
 *   - 视觉风格完全对齐 Tailwind 暗色主题
 *
 * 行级 diff 算法用简单的 LCS（最长公共子序列）。对单文件几千行内的对比
 * 性能完全够；编排器已经把大文件截断了。
 */

import { useMemo, useState } from "react";
import { ChevronDown, ChevronRight, FileText, FilePlus, FileMinus, FileWarning } from "lucide-react";
import type { FileDiff } from "@/lib/workflow-types";

interface Props {
  diffs: FileDiff[];
  collapsed?: boolean;  // 默认全部折叠（点击展开）
}

export function DiffViewer({ diffs, collapsed }: Props) {
  if (!diffs || diffs.length === 0) {
    return (
      <div className="text-xs text-fg-subtle p-3">
        这一步没有文件变更。
      </div>
    );
  }
  return (
    <div className="space-y-2">
      {diffs.map((d, i) => (
        <DiffFile key={i} diff={d} defaultExpanded={!collapsed} />
      ))}
    </div>
  );
}

// ─────────────── 单文件 ───────────────

function DiffFile({ diff, defaultExpanded }: { diff: FileDiff; defaultExpanded: boolean }) {
  const [expanded, setExpanded] = useState(defaultExpanded);

  const Icon = useMemo(() => {
    switch (diff.kind) {
      case "created":
        return FilePlus;
      case "deleted":
        return FileMinus;
      case "modified":
        return FileText;
      default:
        return FileWarning;
    }
  }, [diff.kind]);

  const kindColor = {
    created: "text-success",
    deleted: "text-error",
    modified: "text-accent",
  }[diff.kind] || "text-fg-muted";

  const lines = useMemo(() => alignLines(diff.before || "", diff.after || ""), [
    diff.before,
    diff.after,
  ]);

  return (
    <div className="rounded-md border border-border overflow-hidden bg-canvas">
      <button
        onClick={() => setExpanded((v) => !v)}
        className="w-full px-3 py-2 flex items-center gap-2 text-left bg-surface-hover/40 hover:bg-surface-hover transition-colors"
      >
        <Icon size={14} className={kindColor} />
        <span className="text-xs font-mono text-fg truncate flex-1">
          {diff.path}
        </span>
        <span className={`text-xs ${kindColor}`}>{diff.kind}</span>
        {expanded ? (
          <ChevronDown size={12} className="text-fg-subtle" />
        ) : (
          <ChevronRight size={12} className="text-fg-subtle" />
        )}
      </button>

      {expanded && (
        <div className="overflow-x-auto">
          <table className="text-xs font-mono w-full border-collapse">
            <tbody>
              {lines.map((row, i) => (
                <tr key={i}>
                  <td
                    className={`px-2 py-0.5 align-top whitespace-pre w-1/2 border-r border-border/40 ${
                      row.left.kind === "removed"
                        ? "bg-error/10 text-fg"
                        : row.left.kind === "unchanged"
                          ? "text-fg-muted"
                          : "text-fg-subtle"
                    }`}
                  >
                    <span className="select-none text-fg-subtle mr-2 inline-block w-8 text-right">
                      {row.left.lineNo ?? ""}
                    </span>
                    {row.left.content}
                  </td>
                  <td
                    className={`px-2 py-0.5 align-top whitespace-pre w-1/2 ${
                      row.right.kind === "added"
                        ? "bg-success/10 text-fg"
                        : row.right.kind === "unchanged"
                          ? "text-fg-muted"
                          : "text-fg-subtle"
                    }`}
                  >
                    <span className="select-none text-fg-subtle mr-2 inline-block w-8 text-right">
                      {row.right.lineNo ?? ""}
                    </span>
                    {row.right.content}
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {diff.kind === "modified" && (diff as FileDiff & { truncated?: boolean }).truncated && (
        <div className="px-3 py-1 text-xs text-warning">
          ⚠ 文件较大，已截断。完整内容请查 git diff。
        </div>
      )}
    </div>
  );
}

// ─────────────── LCS 行级对齐 ───────────────

interface Cell {
  kind: "added" | "removed" | "unchanged" | "blank";
  content: string;
  lineNo?: number;
}

interface AlignedRow {
  left: Cell;
  right: Cell;
}

const BLANK: Cell = { kind: "blank", content: "" };

/**
 * LCS 对齐两份文本行，生成 side-by-side 行表。
 *
 * 简单 O(N×M) DP。极端长文件（编排器已截断到 ~100KB）下也只是 ~几千×几千 = 10^7，
 * 浏览器一次性渲染完全可接受。
 */
function alignLines(before: string, after: string): AlignedRow[] {
  const a = before.split("\n");
  const b = after.split("\n");
  const n = a.length;
  const m = b.length;

  // dp[i][j] = LCS 长度
  // 用 1D 滚动可以省内存但代码复杂，行内长度 < 10000 时不重要
  const dp: number[][] = Array.from({ length: n + 1 }, () => new Array(m + 1).fill(0));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      if (a[i] === b[j]) {
        dp[i][j] = dp[i + 1][j + 1] + 1;
      } else {
        dp[i][j] = Math.max(dp[i + 1][j], dp[i][j + 1]);
      }
    }
  }

  const rows: AlignedRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) {
      rows.push({
        left: { kind: "unchanged", content: a[i], lineNo: i + 1 },
        right: { kind: "unchanged", content: b[j], lineNo: j + 1 },
      });
      i++;
      j++;
    } else if (dp[i + 1][j] >= dp[i][j + 1]) {
      rows.push({
        left: { kind: "removed", content: a[i], lineNo: i + 1 },
        right: BLANK,
      });
      i++;
    } else {
      rows.push({
        left: BLANK,
        right: { kind: "added", content: b[j], lineNo: j + 1 },
      });
      j++;
    }
  }
  while (i < n) {
    rows.push({
      left: { kind: "removed", content: a[i], lineNo: i + 1 },
      right: BLANK,
    });
    i++;
  }
  while (j < m) {
    rows.push({
      left: BLANK,
      right: { kind: "added", content: b[j], lineNo: j + 1 },
    });
    j++;
  }
  return rows;
}

import { useState } from "react";
import { useTranslation } from "react-i18next";

type Props = {
  total: number;
  page: number;
  pageSize: number;
  onPageChange: (page: number) => void;
};

export default function Pagination({ total, page, pageSize, onPageChange }: Props) {
  const { t } = useTranslation("cases");
  const totalPages = Math.max(1, Math.ceil(total / pageSize));
  const [jumpVal, setJumpVal] = useState("");
  const canPrev = page > 1;
  const canNext = page < totalPages;

  const commitJump = () => {
    const n = parseInt(jumpVal, 10);
    if (!Number.isFinite(n)) return;
    const clamped = Math.max(1, Math.min(totalPages, n));
    onPageChange(clamped);
    setJumpVal("");
  };

  return (
    <div
      className="pagination"
      style={{
        display: "flex",
        gap: 12,
        alignItems: "center",
        padding: "8px 12px",
        borderTop: "1px solid var(--border-1)",
      }}
      data-testid="pagination"
    >
      <button
        type="button"
        className="btn sm"
        disabled={!canPrev}
        onClick={() => onPageChange(page - 1)}
        aria-label={t("pagination.ariaPrev")}
        data-testid="pagination-prev"
      >
        {t("pagination.prev")}
      </button>
      <span data-testid="pagination-page-of-total">
        {t("pagination.pageOfTotal", { page, total_pages: totalPages })}
      </span>
      <button
        type="button"
        className="btn sm"
        disabled={!canNext}
        onClick={() => onPageChange(page + 1)}
        aria-label={t("pagination.ariaNext")}
        data-testid="pagination-next"
      >
        {t("pagination.next")}
      </button>
      <span style={{ color: "var(--ink-4)" }} data-testid="pagination-total">
        {t("pagination.totalCount", { total })}
      </span>
      <span style={{ marginLeft: "auto", display: "flex", gap: 4 }}>
        <input
          type="number"
          min={1}
          max={totalPages}
          value={jumpVal}
          onChange={(e) => setJumpVal(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && commitJump()}
          aria-label={t("pagination.ariaJumpInput")}
          data-testid="pagination-jump-input"
          style={{ width: 64 }}
        />
        <button
          type="button"
          className="btn sm"
          onClick={commitJump}
          data-testid="pagination-jump-go"
        >
          {t("pagination.jumpTo")}
        </button>
      </span>
    </div>
  );
}

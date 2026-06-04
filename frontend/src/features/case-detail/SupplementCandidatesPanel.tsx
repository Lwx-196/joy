import { Link } from "react-router-dom";

import type { SupplementCandidate, SupplementGap } from "../../api";

type SupplementCandidatesPanelProps = {
  isLoading: boolean;
  isError: boolean;
  gaps: SupplementGap[] | null;
  message: string | null;
  isCopying: boolean;
  onCopyCandidate: (gap: SupplementGap, candidate: SupplementCandidate) => void;
};

export function SupplementCandidatesPanel({
  isLoading,
  isError,
  gaps,
  message,
  isCopying,
  onCopyCandidate,
}: SupplementCandidatesPanelProps) {
  return (
    <div className="supplement-panel">
      {isLoading && <div className="empty">正在从全局真实照片队列查找候选…</div>}
      {isError && <div className="empty">补图候选加载失败</div>}
      {gaps && gaps.length === 0 && (
        <div className="empty">当前三联槽位已配齐，无需跨案例补图</div>
      )}
      {gaps?.map((gap) => (
        <div key={gap.key} className="supplement-gap">
          <div className="supplement-gap-head">
            <div>
              <b>{gap.view_label} · {gap.role_label}</b>
              <span>
                {gap.body_part === "body" ? "身体" : gap.body_part === "face" ? "面部" : "部位未识别"}
                {gap.treatment_area ? ` / ${gap.treatment_area}` : ""}
              </span>
            </div>
            <em>{gap.candidate_count ?? 0} 个候选</em>
          </div>
          <div className="supplement-candidate-grid">
            {(gap.candidates ?? []).map((candidate) => (
              <article key={`${gap.key}-${candidate.case_id}-${candidate.filename}`} className="supplement-candidate-card">
                <img src={candidate.preview_url} alt={candidate.filename} loading="lazy" />
                <div className="supplement-candidate-body">
                  <b title={candidate.filename}>{candidate.filename}</b>
                  <span>{candidate.case_title}</span>
                  <em>{candidate.match_reasons.join(" / ")}</em>
                  <div>
                    <Link to={`/cases/${candidate.case_id}`}>来源 #{candidate.case_id}</Link>
                    <button
                      type="button"
                      className="btn sm primary"
                      onClick={() => onCopyCandidate(gap, candidate)}
                      disabled={isCopying}
                      title="复制到当前案例，并标记为补图待确认"
                    >
                      {isCopying ? "复制中…" : "复制到本案"}
                    </button>
                  </div>
                </div>
              </article>
            ))}
            {(gap.candidates ?? []).length === 0 && (
              <div className="empty">没有找到安全候选：低置信、需换片、已排除出图的照片已被过滤</div>
            )}
          </div>
        </div>
      ))}
      {message && <div className="supplement-message">{message}</div>}
    </div>
  );
}

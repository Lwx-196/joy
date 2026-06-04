import { useEffect, useState } from "react";

import { resolveCandidates, type CandidateResult, type Category } from "../../api";

export type CaseDetailDraft = {
  manual_category: "" | Category;
  manual_template_tier: string;
  notes: string;
  tags: string;
  extra_blocking: string[];
};

type DraftSource = {
  manual_category: Category | null;
  manual_template_tier: string | null;
  notes: string | null;
  tags: string[];
  manual_blocking_codes: string[];
};

type CustomerSource = {
  customer_id: number | null;
  customer_raw: string | null;
};

const EMPTY_DRAFT: CaseDetailDraft = {
  manual_category: "",
  manual_template_tier: "",
  notes: "",
  tags: "",
  extra_blocking: [],
};

export function useCaseDetailDraft(data: DraftSource | null, editing: boolean) {
  const [draft, setDraft] = useState<CaseDetailDraft>(EMPTY_DRAFT);

  useEffect(() => {
    if (!data) return;
    if (editing) return;
    // Sync the read-only case payload into the local edit draft when leaving edit mode.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setDraft({
      manual_category: data.manual_category ?? "",
      manual_template_tier: data.manual_template_tier ?? "",
      notes: data.notes ?? "",
      tags: data.tags.join(", "),
      extra_blocking: data.manual_blocking_codes,
    });
  }, [data, editing]);

  return [draft, setDraft] as const;
}

export function useCustomerCandidates(data: CustomerSource | null): CandidateResult | null {
  const [candidates, setCandidates] = useState<CandidateResult | null>(null);

  useEffect(() => {
    if (!data) return;
    if (!data.customer_id && data.customer_raw) {
      let cancelled = false;
      resolveCandidates(data.customer_raw).then((res) => {
        if (!cancelled) setCandidates(res);
      });
      return () => {
        cancelled = true;
      };
    }
    // Clear stale suggestions after the case becomes bound or loses a raw name.
    // eslint-disable-next-line react-hooks/set-state-in-effect
    setCandidates(null);
  }, [data]);

  return candidates;
}

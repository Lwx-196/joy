import { lazy } from "react";
import { Routes, Route, Navigate, useParams } from "react-router-dom";
import Layout from "./components/Layout";
import Dashboard from "./pages/Dashboard";
import { UndoToast } from "./lib/undo-toast";
import { BatchJobToast } from "./lib/batch-job-toast";

const Cases = lazy(() => import("./pages/Cases"));
const CaseDetail = lazy(() => import("./pages/CaseDetail"));
const Customers = lazy(() => import("./pages/Customers"));
const CustomerDetail = lazy(() => import("./pages/CustomerDetail"));
const Dict = lazy(() => import("./pages/Dict"));
const Evaluations = lazy(() => import("./pages/Evaluations"));
const JobBatch = lazy(() => import("./pages/JobBatch"));

/** Permanent redirect from the legacy /render/batches/:batchId path to the
 * unified /jobs/batches/:batchId?type=render. Bookmarks and old links keep
 * working. */
function LegacyRenderBatchRedirect() {
  const { batchId } = useParams<{ batchId: string }>();
  return <Navigate to={`/jobs/batches/${batchId}?type=render`} replace />;
}

export default function App() {
  return (
    <>
      <Routes>
        <Route element={<Layout />}>
          <Route index element={<Dashboard />} />
          <Route path="cases" element={<Cases />} />
          <Route path="cases/:id" element={<CaseDetail />} />
          <Route path="customers" element={<Customers />} />
          <Route path="customers/:id" element={<CustomerDetail />} />
          <Route path="dict" element={<Dict />} />
          <Route path="evaluations" element={<Evaluations />} />
          <Route path="jobs/batches/:batchId" element={<JobBatch />} />
          <Route
            path="render/batches/:batchId"
            element={<LegacyRenderBatchRedirect />}
          />
          <Route path="*" element={<Navigate to="/" replace />} />
        </Route>
      </Routes>
      {/* Global 30s undo toast — listens for ⌘Z anywhere in the app. */}
      <UndoToast />
      {/* Global batch job toast — stacks render + upgrade batches at bottom-right. */}
      <BatchJobToast />
    </>
  );
}

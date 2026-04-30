import i18n from "i18next";
import { initReactI18next } from "react-i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import zhCommon from "../locales/zh/common.json";
import zhCustomers from "../locales/zh/customers.json";
import zhRender from "../locales/zh/render.json";
import zhRevisions from "../locales/zh/revisions.json";
import zhCustomerDetail from "../locales/zh/customerDetail.json";
import zhEvaluations from "../locales/zh/evaluations.json";
import zhDict from "../locales/zh/dict.json";
import zhJobBatch from "../locales/zh/jobBatch.json";
import zhDashboard from "../locales/zh/dashboard.json";
import zhCases from "../locales/zh/cases.json";
import zhCaseDetail from "../locales/zh/caseDetail.json";
import zhImportCsv from "../locales/zh/importCsv.json";
import zhHotkeys from "../locales/zh/hotkeys.json";
import zhRenderHistory from "../locales/zh/renderHistory.json";
import enCommon from "../locales/en/common.json";
import enCustomers from "../locales/en/customers.json";
import enRender from "../locales/en/render.json";
import enRevisions from "../locales/en/revisions.json";
import enCustomerDetail from "../locales/en/customerDetail.json";
import enEvaluations from "../locales/en/evaluations.json";
import enDict from "../locales/en/dict.json";
import enJobBatch from "../locales/en/jobBatch.json";
import enDashboard from "../locales/en/dashboard.json";
import enCases from "../locales/en/cases.json";
import enCaseDetail from "../locales/en/caseDetail.json";
import enImportCsv from "../locales/en/importCsv.json";
import enHotkeys from "../locales/en/hotkeys.json";
import enRenderHistory from "../locales/en/renderHistory.json";

void i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    resources: {
      zh: {
        common: zhCommon,
        customers: zhCustomers,
        render: zhRender,
        revisions: zhRevisions,
        customerDetail: zhCustomerDetail,
        evaluations: zhEvaluations,
        dict: zhDict,
        jobBatch: zhJobBatch,
        dashboard: zhDashboard,
        cases: zhCases,
        caseDetail: zhCaseDetail,
        importCsv: zhImportCsv,
        hotkeys: zhHotkeys,
        renderHistory: zhRenderHistory,
      },
      en: {
        common: enCommon,
        customers: enCustomers,
        render: enRender,
        revisions: enRevisions,
        customerDetail: enCustomerDetail,
        evaluations: enEvaluations,
        dict: enDict,
        jobBatch: enJobBatch,
        dashboard: enDashboard,
        cases: enCases,
        caseDetail: enCaseDetail,
        importCsv: enImportCsv,
        hotkeys: enHotkeys,
        renderHistory: enRenderHistory,
      },
    },
    fallbackLng: "zh",
    supportedLngs: ["zh", "en"],
    defaultNS: "common",
    ns: ["common", "customers", "render", "revisions", "customerDetail", "evaluations", "dict", "jobBatch", "dashboard", "cases", "caseDetail", "importCsv", "hotkeys", "renderHistory"],
    interpolation: { escapeValue: false },
    react: { useSuspense: false },
    detection: {
      order: ["localStorage", "navigator", "htmlTag"],
      caches: ["localStorage"],
      lookupLocalStorage: "i18nextLng",
    },
  });

export default i18n;

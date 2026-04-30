import "i18next";
import common from "../locales/zh/common.json";
import customers from "../locales/zh/customers.json";
import render from "../locales/zh/render.json";
import revisions from "../locales/zh/revisions.json";
import customerDetail from "../locales/zh/customerDetail.json";
import evaluations from "../locales/zh/evaluations.json";
import dict from "../locales/zh/dict.json";
import jobBatch from "../locales/zh/jobBatch.json";
import dashboard from "../locales/zh/dashboard.json";
import cases from "../locales/zh/cases.json";
import caseDetail from "../locales/zh/caseDetail.json";
import importCsv from "../locales/zh/importCsv.json";
import hotkeys from "../locales/zh/hotkeys.json";
import renderHistory from "../locales/zh/renderHistory.json";

declare module "i18next" {
  interface CustomTypeOptions {
    defaultNS: "common";
    resources: {
      common: typeof common;
      customers: typeof customers;
      render: typeof render;
      revisions: typeof revisions;
      customerDetail: typeof customerDetail;
      evaluations: typeof evaluations;
      dict: typeof dict;
      jobBatch: typeof jobBatch;
      dashboard: typeof dashboard;
      cases: typeof cases;
      caseDetail: typeof caseDetail;
      importCsv: typeof importCsv;
      hotkeys: typeof hotkeys;
      renderHistory: typeof renderHistory;
    };
  }
}

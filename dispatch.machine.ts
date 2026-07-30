import {
  and,
  boolType,
  defineMachine,
  enumType,
  eq,
  forall,
  index,
  lit,
  mapVar,
  modelValues,
  not,
  or,
  param,
  setMap,
  variable,
} from "tla-precheck";

const intakeExists = variable("intakeExists");
const fixExists = variable("fixExists");
const selectedKind = variable("selectedKind");
const prOpen = variable("prOpen");

export const dispatchMachine = defineMachine({
  version: 2,
  moduleName: "DispatchHandoff",
  variables: {
    intakeExists: mapVar("Issues", boolType(), lit(false)),
    fixExists: mapVar("Issues", boolType(), lit(false)),
    selectedKind: mapVar("Issues", enumType("none", "intake", "fix"), lit("none")),
    prOpen: mapVar("Issues", boolType(), lit(false)),
  },
  actions: {
    createIntakeTask: {
      params: { issue: "Issues" },
      guard: not(index(intakeExists, param("issue"))),
      updates: [setMap("intakeExists", param("issue"), lit(true))],
    },
    selectIntakeTask: {
      params: { issue: "Issues" },
      guard: and(
        index(intakeExists, param("issue")),
        not(index(fixExists, param("issue"))),
        eq(index(selectedKind, param("issue")), lit("none"))
      ),
      updates: [setMap("selectedKind", param("issue"), lit("intake"))],
    },
    handoffToFixTask: {
      params: { issue: "Issues" },
      guard: eq(index(selectedKind, param("issue")), lit("intake")),
      updates: [
        setMap("fixExists", param("issue"), lit(true)),
        setMap("selectedKind", param("issue"), lit("none")),
      ],
    },
    selectFixTask: {
      params: { issue: "Issues" },
      guard: and(
        index(fixExists, param("issue")),
        eq(index(selectedKind, param("issue")), lit("none"))
      ),
      updates: [setMap("selectedKind", param("issue"), lit("fix"))],
    },
    openPrFromFixTask: {
      params: { issue: "Issues" },
      guard: and(
        eq(index(selectedKind, param("issue")), lit("fix")),
        not(index(prOpen, param("issue")))
      ),
      updates: [setMap("prOpen", param("issue"), lit(true))],
    },
  },
  invariants: {
    fixTaskSupersedesIntakeSelection: {
      description: "A canonical fix task prevents selection of the stale intake task",
      formula: forall("Issues", "issue", or(
        not(index(fixExists, param("issue"))),
        not(eq(index(selectedKind, param("issue")), lit("intake")))
      )),
    },
    prRequiresSelectedFixTask: {
      description: "Every open PR was opened by selected canonical fix work",
      formula: forall("Issues", "issue", or(
        not(index(prOpen, param("issue"))),
        eq(index(selectedKind, param("issue")), lit("fix"))
      )),
    },
  },
  proof: {
    defaultTier: "pr",
    tiers: {
      pr: {
        domains: { Issues: modelValues("issue", { size: 2, symmetry: true }) },
        budgets: { maxEstimatedStates: 10000 },
        checks: { deadlock: false },
      },
    },
  },
});

export default dispatchMachine;

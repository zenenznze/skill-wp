export type JevDisposition = "complete" | "retry" | "blocked";

export interface SemanticEvidence {
  goal: string;
  acceptanceCriteria: readonly string[];
  executorSummary: string;
  validationResults: readonly string[];
  blockers: readonly string[];
}

export interface JevThresholds {
  minimumConfidence: number;
  minimumProbability: number;
}

export interface JevChoiceAnswer {
  /** Required in API responses; optional here so test/build callers can construct fixtures. */
  type?: "choice";
  choice: JevDisposition;
  probabilities: Record<JevDisposition, number>;
  confidence: number;
}

export interface TypeSafeChoiceQuestion {
  type: "choice";
  instructions: string;
  criteria: Record<JevDisposition, string>;
}

export interface TypeSafeRequest {
  model: "jev-latest";
  state: SemanticEvidence;
  questions: { disposition: TypeSafeChoiceQuestion };
}

export interface TypeSafeResponse {
  answers?: { disposition?: JevChoiceAnswer };
}

export const TYPESAFE_SYSTEM_ONE_URL = "https://api.typesafe.ai/v1/systemone";

export type TypeSafeTransport = (
  request: TypeSafeRequest,
  apiKey: string,
) => Promise<TypeSafeResponse>;

export interface JevBoundaryOptions {
  transport?: TypeSafeTransport;
  thresholds?: Partial<JevThresholds>;
  env?: Readonly<Record<string, string | undefined>>;
}

export interface JevBoundaryResult {
  judgment: JevDisposition | "manual_review";
  probability: number | null;
  confidence: number | null;
  preserveResources: boolean;
  reason:
    | "accepted"
    | "deterministic_gate_failed"
    | "missing_api_key"
    | "api_failure"
    | "malformed_response"
    | "low_confidence";
}

export const DEFAULT_JEV_THRESHOLDS: Readonly<JevThresholds> = Object.freeze({
  minimumConfidence: 0.75,
  minimumProbability: 0.70,
});

const DISPOSITIONS = ["complete", "retry", "blocked"] as const;

const DISPOSITION_QUESTION: TypeSafeChoiceQuestion = {
  type: "choice",
  instructions:
    "Given the repository task evidence in state, which disposition should the controller take? Judge only the supplied acceptance criteria, validation results, executor summary, and blockers.",
  criteria: {
    complete:
      "All acceptance criteria are supported by successful validation evidence, with no unresolved blocker.",
    retry:
      "The task is not yet acceptable, but another bounded implementation attempt can plausibly resolve the failures.",
    blocked:
      "The task cannot proceed without external input, unavailable access, or a dependency outside the executor's control.",
  },
};

function checked(value: number, name: string): number {
  if (!Number.isFinite(value) || value < 0 || value > 1) {
    throw new RangeError(`${name} must be between 0 and 1`);
  }
  return value;
}

function failSafe(
  reason: Exclude<JevBoundaryResult["reason"], "accepted">,
  answer?: Partial<JevChoiceAnswer>,
): JevBoundaryResult {
  const selected = answer?.choice;
  const probability =
    selected && answer?.probabilities && typeof answer.probabilities[selected] === "number"
      ? answer.probabilities[selected]
      : null;
  return {
    judgment: "manual_review",
    probability,
    confidence: typeof answer?.confidence === "number" ? answer.confidence : null,
    preserveResources: true,
    reason,
  };
}

function validUnitInterval(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value) && value >= 0 && value <= 1;
}

function validAnswer(value: unknown): value is JevChoiceAnswer {
  if (!value || typeof value !== "object") return false;
  const answer = value as Partial<JevChoiceAnswer>;
  // Injected transports may use legacy fixtures without `type`; live responses must not
  // be accepted when they declare a different primitive.
  if ((answer.type !== undefined && answer.type !== "choice") || !DISPOSITIONS.includes(answer.choice as JevDisposition)) return false;
  if (!validUnitInterval(answer.confidence) || !answer.probabilities) return false;

  const probabilities = DISPOSITIONS.map((choice) => answer.probabilities?.[choice]);
  return probabilities.every(validUnitInterval);
}

export const fetchTypeSafe: TypeSafeTransport = async (request, apiKey) => {
  const response = await fetch(process.env.TYPESAFE_BASE_URL || TYPESAFE_SYSTEM_ONE_URL, {
    method: "POST",
    headers: {
      Authorization: `Bearer ${apiKey}`,
      "Content-Type": "application/json",
    },
    body: JSON.stringify(request),
  });
  if (!response.ok) throw new Error(`TypeSafe request failed with status ${response.status}`);
  return (await response.json()) as TypeSafeResponse;
};

/** Advisory only: semantic output can never override deterministic acceptance. */
export async function judgeWithJev(
  evidence: SemanticEvidence,
  deterministicGatePassed: boolean,
  options: JevBoundaryOptions = {},
): Promise<JevBoundaryResult> {
  if (!deterministicGatePassed) return failSafe("deterministic_gate_failed");

  const minimumConfidence = checked(
    options.thresholds?.minimumConfidence ?? DEFAULT_JEV_THRESHOLDS.minimumConfidence,
    "minimumConfidence",
  );
  const minimumProbability = checked(
    options.thresholds?.minimumProbability ?? DEFAULT_JEV_THRESHOLDS.minimumProbability,
    "minimumProbability",
  );
  const apiKey = (options.env ?? process.env).TYPESAFE_API_KEY;
  if (!apiKey) return failSafe("missing_api_key");

  let response: TypeSafeResponse;
  try {
    response = await (options.transport ?? fetchTypeSafe)(
      {
        model: "jev-latest",
        state: evidence,
        questions: { disposition: DISPOSITION_QUESTION },
      },
      apiKey,
    );
  } catch {
    return failSafe("api_failure");
  }

  const answer = response.answers?.disposition;
  if (!validAnswer(answer)) return failSafe("malformed_response");
  const probability = answer.probabilities[answer.choice];
  if (answer.confidence < minimumConfidence || probability < minimumProbability) {
    return failSafe("low_confidence", answer);
  }

  return {
    judgment: answer.choice,
    probability,
    confidence: answer.confidence,
    preserveResources: answer.choice !== "complete",
    reason: "accepted",
  };
}

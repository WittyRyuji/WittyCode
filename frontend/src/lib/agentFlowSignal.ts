export const FLOW_SPARK_SPACING = 57;
export const FLOW_SPARK_PERIOD_MS = 540; // 57 flow px per beat = 105.56 flow px/s at 1×.

/** A spur starts this far into the dash pattern supplied by its common trunk. */
export function flowSparkPhase(prefix: number) {
  return prefix % FLOW_SPARK_SPACING;
}

/** The first head already on the wire reaches the target during this beat. */
export function flowArrivalDelay(length: number) {
  return flowSparkPhase(length) / FLOW_SPARK_SPACING * FLOW_SPARK_PERIOD_MS;
}

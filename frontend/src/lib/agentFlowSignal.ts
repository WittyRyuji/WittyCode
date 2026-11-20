export const FLOW_SPARK_SPACING = 57;
export const FLOW_SPARK_PERIOD_MS = 540; // 57 flow px per beat = 105.56 flow px/s at 1×.

/** The first head already on the wire reaches the target during this beat. */
export function flowArrivalDelay(length: number) {
  return (length % FLOW_SPARK_SPACING) / FLOW_SPARK_SPACING * FLOW_SPARK_PERIOD_MS;
}

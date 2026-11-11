/** The sentence to show for a failed call: Python's message for the user, or what went wrong on the way. */
export const messageOf = (error: unknown): string => (error instanceof Error ? error.message : String(error));

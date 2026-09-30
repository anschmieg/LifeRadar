export const READ_ONLY_ERROR_MESSAGE =
  'LifeRadar is read-only: outbound messages are disabled. No message was sent.';

export function rejectOutboundMessage() {
  const error = new Error(READ_ONLY_ERROR_MESSAGE);
  error.code = 'read_only';
  error.statusCode = 403;
  return error;
}

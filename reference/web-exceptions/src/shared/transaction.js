// Executed fixture contract: the response contains an explicit null displayName.
export function mapTransactionDetail(response) {
  return { merchant: response.merchant.displayName.trim() };
}
export function renderTransactionDetail(response) {
  return mapTransactionDetail(response);
}
export function buildTransferView(state) {
  if (!state.recipient) throw new Error('Transfer view requires a selected recipient');
  return { recipientLabel: state.recipient.label };
}

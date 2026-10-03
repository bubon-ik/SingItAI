import { PAY_TO, sameAddress, runTest } from "./payment.mjs";
const $ = id => document.getElementById(id);
$("recipient").textContent = PAY_TO;
const expected = new URL(location.href).searchParams.get("payer");
if (/^0x[0-9a-fA-F]{40}$/.test(expected ?? "")) $("payer").value = expected;
let provider;
let submitted = false;
const providers = [];
window.addEventListener("eip6963:announceProvider", event => {
  if (event.detail?.info?.rdns === "io.metamask") providers.push(event.detail.provider);
});
window.dispatchEvent(new Event("eip6963:requestProvider"));
const status = text => { $("status").textContent = text; };
const attemptKey = () => `singit-ask-test:${$("payer").value.trim().toLowerCase()}`;
const wasSubmitted = () => submitted || sessionStorage.getItem(attemptKey()) === "submitted";

$("connect").onclick = async () => {
  $("connect").disabled = true;
  $("pay").disabled = true;
  try {
    provider = providers[0] ?? window.ethereum?.providers?.find(p => p.isMetaMask && !p.isRabby)
      ?? (window.ethereum?.isMetaMask && !window.ethereum.isRabby ? window.ethereum : null);
    if (!provider) throw new Error("MetaMask was not found. Open this link in a browser with the MetaMask extension, or in the MetaMask app’s browser.");
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    if (!$("payer").value.trim()) $("payer").value = accounts[0] ?? "";
    if (!sameAddress(accounts[0], $("payer").value.trim())) throw new Error("Select the paying wallet shown on this page in MetaMask.");
    if (BigInt(await provider.request({ method: "eth_chainId" })) !== 8453n) {
      await provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: "0x2105" }] });
    }
    if (wasSubmitted()) throw new Error("A payment was already submitted from this tab. Check its result first; another payment is blocked.");
    $("payer").readOnly = true;
    $("pay").disabled = false;
    status("Wallet connected. Review the recipient and amount. Payment starts only after you click Pay and confirm the signature in MetaMask.");
  } catch (error) { status(error.code === 4001 ? "Connection cancelled." : error.message); }
  finally { $("connect").disabled = false; }
};

$("pay").onclick = async () => {
  if (wasSubmitted()) return;
  $("pay").disabled = true;
  $("connect").disabled = true;
  try {
    const result = await runTest({ provider, payer: $("payer").value.trim(), onStatus: status,
      onSubmitting() { sessionStorage.setItem(attemptKey(), "submitted"); submitted = true; } });
    $("result").hidden = false;
    $("answer").textContent = result.answer;
    $("transaction").href = `https://basescan.org/tx/${result.transaction}`;
    status("Payment confirmed. Paid 0.003 USDC and received the model’s answer.");
  } catch (error) {
    status(error.code === 4001 ? "Signature cancelled. No payment was submitted."
      : (submitted ? "The payment was submitted, but the full result was not received. Do not pay again; report this in the chat. " : "") + error.message);
    if (!submitted) { $("pay").disabled = false; $("connect").disabled = false; }
  }
};

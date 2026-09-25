// Where the page finds the SingIt web API (docs/allowance-web-v1.md).
// The web API serves this page too (https://app.singitai.app/app/), so the API
// is on the same origin: "/web/v1".
window.SINGIT_APP_CONFIG = {
  apiBase: "/web/v1",
  // A free project id from cloud.reown.com turns on WalletConnect (mobile
  // wallets by QR code). Empty: browser extensions only.
  walletConnectProjectId: "",
};

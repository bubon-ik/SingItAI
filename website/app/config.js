// Where the page finds the SingIt web API (docs/allowance-web-v1.md).
// The API must be on the same site as this page (api.singitai.app for
// singitai.app) so the session cookie travels with requests.
window.SINGIT_APP_CONFIG = {
  apiBase: "https://api.singitai.app/web/v1",
  // A free project id from cloud.reown.com turns on WalletConnect (mobile
  // wallets by QR code). Empty: browser extensions only.
  walletConnectProjectId: "",
};

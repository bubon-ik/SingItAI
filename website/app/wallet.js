// The user's wallet, through EIP-1193. Browser extensions announce themselves
// by EIP-6963 (Rabby, MetaMask, Phantom…); WalletConnect is added when a
// project id is configured. Everything that gets signed is prepared by the
// server; the page only hands it to the wallet.

const BASE = {
  chainId: "0x2105",
  chainName: "Base",
  nativeCurrency: { name: "Ether", symbol: "ETH", decimals: 18 },
  rpcUrls: ["https://mainnet.base.org"],
  blockExplorerUrls: ["https://basescan.org"],
};

const found = new Map(); // uuid -> { info, provider }

export function discover(onChange) {
  window.addEventListener("eip6963:announceProvider", (event) => {
    const { info, provider } = event.detail || {};
    if (!info || !provider || found.has(info.uuid)) return;
    found.set(info.uuid, { info, provider });
    onChange?.();
  });
  window.dispatchEvent(new Event("eip6963:requestProvider"));
}

export function wallets() {
  const list = [...found.values()];
  // An old-style wallet that does not announce itself.
  if (!list.length && window.ethereum) {
    list.push({ info: { uuid: "injected", name: "Browser wallet", icon: "" }, provider: window.ethereum });
  }
  return list;
}

export function walletConnectAvailable() {
  return Boolean(window.SINGIT_APP_CONFIG?.walletConnectProjectId);
}

async function walletConnectProvider() {
  const { EthereumProvider } = await import("https://esm.sh/@walletconnect/ethereum-provider@2.17.0");
  const provider = await EthereumProvider.init({
    projectId: window.SINGIT_APP_CONFIG.walletConnectProjectId,
    chains: [8453],
    showQrModal: true,
    metadata: {
      name: "SingIt",
      description: "Agent allowance",
      url: location.origin,
      icons: [new URL("../assets/favicon.svg", location.href).href],
    },
  });
  await provider.connect();
  return provider;
}

export class Wallet {
  constructor(provider, name) {
    this.provider = provider;
    this.name = name;
    this.address = null;
  }

  static async connect(choice) {
    const provider = choice === "walletconnect" ? await walletConnectProvider() : choice.provider;
    const wallet = new Wallet(provider, choice === "walletconnect" ? "WalletConnect" : choice.info.name);
    const accounts = await provider.request({ method: "eth_requestAccounts" });
    if (!accounts?.length) throw new Error("The wallet shared no account.");
    wallet.address = accounts[0];
    await wallet.ensureBase();
    return wallet;
  }

  on(event, handler) {
    this.provider.on?.(event, handler);
  }

  async ensureBase() {
    const chainId = await this.provider.request({ method: "eth_chainId" });
    if (String(chainId).toLowerCase() === BASE.chainId) return;
    try {
      await this.provider.request({ method: "wallet_switchEthereumChain", params: [{ chainId: BASE.chainId }] });
    } catch (error) {
      if (error?.code !== 4902) throw error;
      await this.provider.request({ method: "wallet_addEthereumChain", params: [BASE] });
    }
  }

  async signMessage(message) {
    const hex = "0x" + [...new TextEncoder().encode(message)].map((b) => b.toString(16).padStart(2, "0")).join("");
    return this.provider.request({ method: "personal_sign", params: [hex, this.address] });
  }

  async sendTransaction(tx) {
    await this.ensureBase();
    return this.provider.request({
      method: "eth_sendTransaction",
      params: [{ from: this.address, to: tx.to, data: tx.data, value: tx.value || "0x0" }],
    });
  }

  async signTypedData(typedData) {
    await this.ensureBase();
    return this.provider.request({
      method: "eth_signTypedData_v4",
      params: [this.address, JSON.stringify(typedData)],
    });
  }
}

export function walletError(error) {
  if (error?.code === 4001 || /reject|denied|cancel/i.test(error?.message || "")) {
    return "You cancelled it in your wallet. Nothing changed.";
  }
  return error?.message || String(error);
}

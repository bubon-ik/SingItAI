// The user's wallet, through EIP-1193.
//
// With a WalletConnect project id (config.js) the connection goes through
// Reown AppKit — the standard wallet modal: browser extensions, WalletConnect
// QR for mobile wallets, and a session that survives a reload. Without one,
// the page lists the extensions that announce themselves by EIP-6963.
// Everything that gets signed is prepared by the server; the page only hands
// it to the wallet.

const APPKIT = "https://cdn.jsdelivr.net/npm/@reown/appkit-cdn@1.8.24/dist/appkit.js";
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
  if (!list.length && window.ethereum) {
    list.push({ info: { uuid: "injected", name: "Browser wallet", icon: "" }, provider: window.ethereum });
  }
  return list;
}

export class Wallet {
  constructor(provider, name, address) {
    this.provider = provider;
    this.name = name;
    this.address = address || null;
  }

  static async fromInjected(choice) {
    const wallet = new Wallet(choice.provider, choice.info.name);
    const accounts = await wallet.provider.request({ method: "eth_requestAccounts" });
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
    return this.provider.request({ method: "eth_signTypedData_v4", params: [this.address, JSON.stringify(typedData)] });
  }
}

// -- Reown AppKit (WalletConnect) --

let kit = null;

export function appKitConfigured() {
  return Boolean(window.SINGIT_APP_CONFIG?.walletConnectProjectId);
}

async function appKit() {
  if (kit) return kit;
  const { createAppKit, WagmiAdapter, networks } = await import(APPKIT);
  const projectId = window.SINGIT_APP_CONFIG.walletConnectProjectId;
  const adapter = new WagmiAdapter({ projectId, networks: [networks.base] });
  kit = createAppKit({
    adapters: [adapter],
    networks: [networks.base],
    defaultNetwork: networks.base,
    projectId,
    metadata: {
      name: "SingIt",
      description: "Give your AI agent an allowance from your own wallet.",
      url: location.origin,
      icons: [new URL("../assets/favicon.svg", location.href).href],
    },
    themeMode: "dark",
    themeVariables: {
      "--w3m-accent": "#3ecf8e",
      "--w3m-color-mix": "#050505",
      "--w3m-color-mix-strength": 25,
      "--w3m-font-family": "Geist, 'Helvetica Neue', sans-serif",
      "--w3m-border-radius-master": "3px",
    },
    features: { analytics: false, email: false, socials: false, swaps: false, onramp: false, send: false, history: false },
    allowUnsupportedChain: false,
  });
  return kit;
}

function kitWallet(k, address) {
  const provider = k.getWalletProvider?.() || k.getProvider?.("eip155");
  if (!provider) throw new Error("The wallet connected but gave no provider. Try again.");
  return new Wallet(provider, "WalletConnect", address);
}

// Watch AppKit: a restored session, a new connection, a switch or a disconnect.
export async function watchAppKit(onWallet) {
  const k = await appKit();
  k.subscribeAccount((account) => {
    if (account?.isConnected && account.address) {
      try { onWallet(kitWallet(k, account.address)); } catch { /* provider not ready yet; the next event has it */ }
    } else if (account && account.status === "disconnected") {
      onWallet(null);
    }
  });
}

export async function openAppKit() {
  const k = await appKit();
  await k.open({ view: "Connect" });
}

export async function disconnectAppKit() {
  if (kit) await kit.disconnect?.();
}

export function walletError(error) {
  if (error?.code === 4001 || /reject|denied|cancel/i.test(error?.message || "")) {
    return "You cancelled it in your wallet. Nothing changed.";
  }
  return error?.message || String(error);
}

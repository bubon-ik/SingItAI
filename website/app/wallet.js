// The user's wallet: an EVM one through EIP-1193, or a Solana one (Phantom,
// Solflare, Backpack…) through its signMessage — or, with no wallet at all, the
// Reown embedded wallet created by signing in with email, Google, Apple, X or Discord.
//
// With a WalletConnect project id (config.js) the connection goes through
// Reown AppKit — the standard wallet modal, for EVM and Solana: browser
// extensions, WalletConnect QR for mobile wallets, and a session that survives
// a reload. Without one, the page lists the EVM extensions that announce
// themselves by EIP-6963 and the Solana extensions it finds on the page.
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

// Installed extensions the wallet list shows: the ones most people have. Others (Keplr,
// Rainbow, Ambire…) are left out of the list; WalletConnect's QR still connects them.
// Filtering the EIP-6963 announcement itself is the only way that also covers
// extensions WalletConnect's directory does not know.
const SHOWN_EXTENSIONS = new Set([
  "io.metamask", "app.phantom", "io.rabby", "com.coinbase.wallet", "com.trustwallet.app",
  "com.okex.wallet", "com.binance.wallet", "app.backpack", "com.brave.wallet",
]);
window.addEventListener("eip6963:announceProvider", (event) => {
  const rdns = event.detail?.info?.rdns;
  if (rdns && !SHOWN_EXTENSIONS.has(rdns)) event.stopImmediatePropagation();
}, { capture: true });

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

// Solana extensions that put a provider on the page.
const SOLANA_INJECTED = [
  ["sol-phantom", "Phantom (Solana)", () => window.phantom?.solana],
  ["sol-solflare", "Solflare", () => window.solflare],
  ["sol-backpack", "Backpack (Solana)", () => window.backpack?.solana || window.backpack],
];

export function wallets() {
  const list = [...found.values()];
  if (!list.length && window.ethereum) {
    list.push({ info: { uuid: "injected", name: "Browser wallet", icon: "" }, provider: window.ethereum });
  }
  for (const [uuid, name, get] of SOLANA_INJECTED) {
    const provider = get();
    if (provider?.connect && provider?.signMessage) list.push({ info: { uuid, name, icon: "" }, provider, chain: "solana" });
  }
  return list;
}

const B58 = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz";

function base58(bytes) {
  let n = 0n;
  for (const b of bytes) n = n * 256n + BigInt(b);
  let out = "";
  while (n > 0n) { out = B58[Number(n % 58n)] + out; n /= 58n; }
  for (const b of bytes) { if (b !== 0) break; out = "1" + out; }
  return out;
}

// @solana/web3.js, only to hand a prepared transaction to the wallet in the shape it expects.
const SOLANA_WEB3 = "https://cdn.jsdelivr.net/npm/@solana/web3.js@1.99.0/+esm";
let web3 = null;
const solanaWeb3 = () => (web3 ||= import(SOLANA_WEB3));
const fromBase64 = (text) => Uint8Array.from(atob(text), (c) => c.charCodeAt(0));
const toBase64 = (bytes) => btoa(Array.from(bytes, (b) => String.fromCharCode(b)).join(""));

// A Solana wallet: signs in (Sign In With Solana) and signs the allowance the server prepares.
export class SolanaWallet {
  constructor(provider, name, address) {
    this.provider = provider;
    this.name = name;
    this.address = address || null;
    this.chain = "solana";
  }

  static async fromInjected(choice) {
    const connected = await choice.provider.connect();
    const key = connected?.publicKey || choice.provider.publicKey;
    if (!key) throw new Error("The wallet shared no account.");
    return new SolanaWallet(choice.provider, choice.info.name, key.toString());
  }

  on(event, handler) {
    // Solana wallets say "accountChanged" with the new public key (or null).
    if (event === "accountsChanged") this.provider.on?.("accountChanged", (key) => handler(key ? [key.toString()] : []));
  }

  async signMessage(message) {
    const signed = await this.provider.signMessage(new TextEncoder().encode(message), "utf8");
    const bytes = signed?.signature || signed;
    if (!(bytes instanceof Uint8Array) || bytes.length !== 64) throw new Error("The wallet returned no signature.");
    return base58(bytes);
  }

  // The approve or revoke the server prepared: the wallet signs it, the server checks it and sends it.
  async signTransaction(base64) {
    // Through AppKit the Solana provider signs only while Solana is the active network
    // (otherwise "Invalid chain id"); the page starts on Base.
    if (this.name === "WalletConnect" && kit && kitNetworks?.solana) await kit.switchNetwork(kitNetworks.solana);
    const { VersionedTransaction } = await solanaWeb3();
    const signed = await this.provider.signTransaction(VersionedTransaction.deserialize(fromBase64(base64)));
    const bytes = signed?.serialize ? signed.serialize() : signed?.signedTransaction?.serialize?.();
    if (!bytes) throw new Error("The wallet returned no signed transaction.");
    return toBase64(bytes);
  }

  unsupported() {
    throw new Error("This is a Base step; your Solana wallet signs its own version of it.");
  }

  async sendTransaction() { this.unsupported(); }
  async signTypedData() { this.unsupported(); }
  async ensureBase() { this.unsupported(); }
}

export class Wallet {
  constructor(provider, name, address) {
    this.provider = provider;
    this.name = name;
    this.address = address || null;
    this.chain = "base";
  }

  static async fromInjected(choice) {
    if (choice.chain === "solana") return SolanaWallet.fromInjected(choice);
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
    if (this.name === "WalletConnect" && kit && kitNetworks?.base && kit.getCaipNetwork?.()?.id !== kitNetworks.base.id) {
      await kit.switchNetwork(kitNetworks.base);
    }
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
    // The sign-in message names Base (Chain ID 8453). Phantom and Reown's email/Google wallet refuse
    // to show it while they sit on another network, so the wallet moves to Base first.
    await this.ensureBase();
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
let kitNetworks = null;

// WalletConnect explorer ids (explorer-api.walletconnect.com).
const POPULAR_WALLETS = {
  metamask: "c57ca95b47569778a828d19178114f4db188b89b763c899ba0be274e97267d96",
  phantom: "a797aa35c0fadbfc1a53e7f675162ed5226968b44a19ee3d24385c64d1d3c393",
  rabby: "18388be9ac2d02726dbac9777c96efaac06d744b2f6d580fccdd4127a6d01fd1",
  coinbase: "fd20dc426fb37566d803205b19bbc1d4096b248ac04548e3cfb6b3a38bd033aa",
  trust: "4622a2b2d6af1c9844944291e5e7351a6aa24cd7b23099efac1b2fd875da31a0",
  okx: "971e689d0a5be527bac79629b4ee9b925e82208e5168b733496a09c0faed0709",
  solflare: "1ca0bdd4747578705b1939af023d120677c64fe6ca76add81fda36e350605e79",
  backpack: "2bd8c14e035c2d48f184aaa168559e86b0e3433228d3c4075900a221785019b0",
};
const HIDDEN_WALLETS = {
  keplr: "6adb6082c909901b9e7189af3a4a0223102cd6f8d5c39e39f3d49acb92b578bb",
  rainbow: "1ae92b26df02f0abca6304df07debccd18262fdf5fe82daa81593582dac9a369",
};

export function appKitConfigured() {
  return Boolean(window.SINGIT_APP_CONFIG?.walletConnectProjectId);
}

async function appKit() {
  if (kit) return kit;
  const { createAppKit, WagmiAdapter, SolanaAdapter, networks } = await import(APPKIT);
  kitNetworks = networks;
  const projectId = window.SINGIT_APP_CONFIG.walletConnectProjectId;
  const adapter = new WagmiAdapter({ projectId, networks: [networks.base] });
  kit = createAppKit({
    adapters: [adapter, new SolanaAdapter()],  // any wallet: EVM on Base, or Solana
    networks: [networks.base, networks.solana],
    defaultNetwork: networks.base,
    projectId,
    metadata: {
      name: "SingIt",
      description: "Give your AI agent an allowance from your own wallet.",
      url: location.origin,
      icons: [new URL("../assets/favicon.svg", location.href).href],
    },
    themeMode: "light",
    themeVariables: {
      "--w3m-accent": "#3ecf8e",
      "--w3m-color-mix": "#f4f2f0",
      "--w3m-color-mix-strength": 25,
      "--w3m-font-family": "Inter, system-ui, sans-serif",
      "--w3m-border-radius-master": "3px",
    },
    // Email and social sign-in create a Reown embedded wallet: no extension, no seed phrase.
    // It must be a plain account (EOA): the limiter's owner signs Sign-In with Ethereum and
    // the allowance, and a smart account would sign through its contract, which v1 refuses.
    defaultAccountTypes: { eip155: "eoa" },
    // The wallets most people have, first; niche ones stay out of the list (Search still finds any).
    featuredWalletIds: Object.values(POPULAR_WALLETS),
    excludeWalletIds: Object.values(HIDDEN_WALLETS),
    features: {
      connectMethodsOrder: ["email", "social", "wallet"],  // new users first: email or Google, then "or a wallet"
      analytics: false, email: true, socials: ["google", "apple", "x", "discord"], emailShowWallets: true,
      swaps: false, onramp: true, send: false, history: false,  // onramp: buy USDC with a card
    },
    allowUnsupportedChain: false,
  });
  return kit;
}

function kitWallet(k, account) {
  const solana = String(account.caipAddress || "").startsWith("solana:");
  const provider = solana
    ? k.getProvider?.("solana") || k.getWalletProvider?.()
    : k.getProvider?.("eip155") || k.getWalletProvider?.();
  if (!provider) throw new Error("The wallet connected but gave no provider. Try again.");
  return solana ? new SolanaWallet(provider, "WalletConnect", account.address) : new Wallet(provider, "WalletConnect", account.address);
}

// Watch AppKit: a restored session, a new connection, a switch or a disconnect.
export async function watchAppKit(onWallet) {
  const k = await appKit();
  let closedFor = null;  // the address the modal was last closed for
  k.subscribeAccount((account) => {
    if (account?.isConnected && account.address) {
      try {
        onWallet(kitWallet(k, account));
        // Connected: the page takes over from the modal, once per address. AppKit sends more events
        // for the same account (network, balance), and closing the modal again would abort the
        // email/Google wallet's sign-in signature shown in it ("Request was aborted").
        if (account.address !== closedFor) {
          closedFor = account.address;
          k.close?.();
        }
      } catch { /* provider not ready yet; the next event has it */ }
    } else if (account && account.status === "disconnected") {
      closedFor = null;
      onWallet(null);
    }
  });
}

export async function openAppKit() {
  const k = await appKit();
  await k.open({ view: "Connect" });
}

// Buy crypto with a card (Reown's onramp providers), for a wallet connected through AppKit.
export async function openOnRamp() {
  const k = await appKit();
  await k.open({ view: "OnRampProviders" });
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

// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// The two ERC-20 functions the limiter uses. The token is USDC, which returns
/// `true` or reverts, so a checked call is enough and no wrapper library is
/// pulled in.
interface IERC20 {
    function transferFrom(address from, address to, uint256 amount) external returns (bool);
    function allowance(address holder, address spender) external view returns (uint256);
}

/// A spending limit the agent cannot exceed. Design: docs/trezor-allowance-v1.md.
///
/// The owner's USDC stay at the owner's hardware-wallet address. The owner
/// grants this contract an ERC-20 allowance with one `approve` from the device;
/// the agent can then move the owner's USDC to a payee only through `spend`,
/// within caps fixed at deployment.
///
/// It holds no tokens, ever: each purchase goes from the owner to the payee in
/// one `transferFrom`. There is nothing here to strand and nothing to steal.
///
/// Nothing can change after deployment — no admin, no proxy, no setter. To
/// change the policy, deploy a new limiter, approve it, and approve the old one
/// down to zero. That keeps every device interaction an ERC-20 `approve`, which
/// the Trezor renders readably (check T2), instead of a call to an unknown
/// contract, which it would show as raw data.
contract AgentAllowance {
    IERC20 public immutable token;
    address public immutable owner;
    address public immutable agent;
    address public immutable guardian;
    uint256 public immutable dailyCap;
    uint256 public immutable perPurchaseCap;
    uint256 public immutable expiry;

    bool public paused;
    uint256 public day;
    uint256 public spentToday;
    mapping(bytes32 => bool) public usedRef;

    event Spent(bytes32 indexed ref, address indexed payee, uint256 amount, uint256 spentToday);
    event Paused(address indexed by);

    error InvalidConfig();
    error NotAgent();
    error NotGuardian();
    error IsPaused();
    error Expired();
    error InvalidPayee();
    error ZeroAmount();
    error InvalidRef();
    error RefUsed();
    error PerPurchaseCapExceeded();
    error DailyCapExceeded();
    error TransferFailed();

    constructor(
        IERC20 token_,
        address owner_,
        address agent_,
        address guardian_,
        uint256 dailyCap_,
        uint256 perPurchaseCap_,
        uint256 expiry_
    ) {
        if (
            address(token_) == address(0) || owner_ == address(0) || agent_ == address(0)
                || guardian_ == address(0) || owner_ == agent_ || owner_ == guardian_
                || agent_ == guardian_ || perPurchaseCap_ == 0 || perPurchaseCap_ > dailyCap_
                || expiry_ <= block.timestamp
        ) revert InvalidConfig();
        token = token_;
        owner = owner_;
        agent = agent_;
        guardian = guardian_;
        dailyCap = dailyCap_;
        perPurchaseCap = perPurchaseCap_;
        expiry = expiry_;
    }

    /// The only function that moves money: `amount` of the owner's tokens to
    /// `payee`, once per `ref`, inside the caps.
    ///
    /// State is written before the transfer, so a payee that re-entered could
    /// not spend the same budget twice. A transfer that fails reverts the whole
    /// call, so a failed purchase consumes neither budget nor `ref`.
    function spend(address payee, uint256 amount, bytes32 ref) external {
        if (msg.sender != agent) revert NotAgent();
        if (paused) revert IsPaused();
        if (block.timestamp >= expiry) revert Expired();
        if (payee == address(0) || payee == address(this) || payee == owner) revert InvalidPayee();
        if (amount == 0) revert ZeroAmount();
        if (amount > perPurchaseCap) revert PerPurchaseCapExceeded();
        if (ref == bytes32(0)) revert InvalidRef();
        if (usedRef[ref]) revert RefUsed();

        // The window resets at UTC midnight, not on a rolling 24 hours: a burst
        // at 23:59 and another at 00:01 spend two caps in two minutes. The
        // allowance, not this counter, bounds the worst case.
        uint256 today = block.timestamp / 1 days;
        uint256 spent = today == day ? spentToday : 0;
        if (spent + amount > dailyCap) revert DailyCapExceeded();

        usedRef[ref] = true;
        day = today;
        spentToday = spent + amount;

        if (!token.transferFrom(owner, payee, amount)) revert TransferFailed();
        emit Spent(ref, payee, amount, spent + amount);
    }

    /// Stops all spending, permanently. There is no unpause: resuming means a
    /// new limiter and a new `approve`, which the owner reads on the device.
    /// The guardian is a hot key that can do this and nothing else, so an
    /// incident can be contained without the owner finding the hardware wallet.
    function pause() external {
        if (msg.sender != guardian && msg.sender != owner) revert NotGuardian();
        paused = true;
        emit Paused(msg.sender);
    }

    /// What the agent may still spend today under the caps. The allowance and
    /// the owner's balance can each be lower; the gateway reads all three.
    function remainingToday() external view returns (uint256) {
        if (paused || block.timestamp >= expiry) return 0;
        uint256 spent = block.timestamp / 1 days == day ? spentToday : 0;
        return dailyCap - spent;
    }

    /// The ERC-20 allowance left: the hard ceiling on everything this contract
    /// can ever move, enforced by the token, not by this code.
    function allowanceLeft() external view returns (uint256) {
        return token.allowance(owner, address(this));
    }
}

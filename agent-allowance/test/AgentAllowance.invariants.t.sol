// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {AgentAllowance, IERC20} from "../src/AgentAllowance.sol";
import {MockUSDC, vm} from "./Support.sol";

/// Drives the limiter with random sequences: purchases of any size, random
/// time jumps, strangers calling `spend`, replayed references. Counts what
/// happened so the invariants can check what must never happen.
contract Handler {
    AgentAllowance public immutable limiter;
    MockUSDC public immutable token;
    address public immutable agent;
    address[3] internal payees = [address(0xB1), address(0xB2), address(0xB3)];

    uint256 public spentTotal;
    uint256 public maxSpentInOneDay;
    uint256 public unauthorisedSuccesses;
    uint256 public replaySuccesses;
    uint256 public oversizedSuccesses;
    mapping(uint256 => uint256) public spentOnDay;

    uint256 internal refCounter = 1;
    bytes32 internal lastRef;

    constructor(AgentAllowance limiter_, MockUSDC token_, address agent_) {
        limiter = limiter_;
        token = token_;
        agent = agent_;
    }

    function purchase(uint256 amountSeed, uint256 payeeSeed, uint256 waitSeed) external {
        vm.warp(block.timestamp + waitSeed % 30 hours);
        uint256 amount = amountSeed % (limiter.perPurchaseCap() * 2);
        bytes32 ref = bytes32(refCounter++);
        vm.prank(agent);
        try limiter.spend(payees[payeeSeed % payees.length], amount, ref) {
            _record(amount, ref);
        } catch {}
    }

    function _record(uint256 amount, bytes32 ref) internal {
        if (amount > limiter.perPurchaseCap()) oversizedSuccesses++;
        uint256 today = block.timestamp / 1 days;
        spentOnDay[today] += amount;
        if (spentOnDay[today] > maxSpentInOneDay) maxSpentInOneDay = spentOnDay[today];
        spentTotal += amount;
        lastRef = ref;
    }

    /// Up to fifteen maximum-size purchases in the same block. Random waits
    /// alone almost never fit ten successful purchases into one UTC day, so
    /// without this the daily-cap invariant would hold without being tested.
    function burst(uint256 countSeed) external {
        uint256 count = 1 + countSeed % 15;
        uint256 amount = limiter.perPurchaseCap();
        for (uint256 i; i < count; ++i) {
            bytes32 ref = bytes32(refCounter++);
            vm.prank(agent);
            try limiter.spend(payees[i % payees.length], amount, ref) {
                _record(amount, ref);
            } catch {}
        }
    }

    function strangerPurchase(address caller, uint256 amountSeed) external {
        if (caller == agent) return;
        uint256 amount = 1 + amountSeed % limiter.perPurchaseCap();
        vm.prank(caller);
        try limiter.spend(payees[0], amount, bytes32(refCounter++)) {
            unauthorisedSuccesses++;
        } catch {}
    }

    function replay() external {
        if (lastRef == bytes32(0)) return;
        vm.prank(agent);
        try limiter.spend(payees[1], 1, lastRef) {
            replaySuccesses++;
        } catch {}
    }
}

contract AgentAllowanceInvariants {
    address constant OWNER = address(0xA11CE);
    address constant AGENT = address(0xA6E47);
    address constant GUARDIAN = address(0x6A4D);
    uint256 constant BALANCE = 1_000_000e6;
    uint256 constant GRANT = 5_000e6;

    MockUSDC token;
    AgentAllowance limiter;
    Handler handler;

    function setUp() public {
        vm.warp(20_000 days);
        token = new MockUSDC();
        token.mint(OWNER, BALANCE);
        limiter = new AgentAllowance(
            IERC20(address(token)), OWNER, AGENT, GUARDIAN, 100e6, 10e6, block.timestamp + 3650 days
        );
        vm.prank(OWNER);
        token.approve(address(limiter), GRANT);
        handler = new Handler(limiter, token, AGENT);
    }

    /// Foundry reads this to fuzz only the handler, not the token's mint.
    function targetContracts() public view returns (address[] memory targets) {
        targets = new address[](1);
        targets[0] = address(handler);
    }

    /// Foundry calls this after each run. Without it, a run in which every
    /// purchase failed would pass every invariant while testing nothing.
    function afterInvariant() public view {
        require(handler.spentTotal() > 0, "no purchase succeeded: the invariants were vacuous");
    }

    function invariant_the_limiter_never_holds_tokens() public view {
        require(token.balanceOf(address(limiter)) == 0, "limiter holds tokens");
    }

    function invariant_no_day_exceeds_the_daily_cap() public view {
        require(handler.maxSpentInOneDay() <= limiter.dailyCap(), "daily cap exceeded");
    }

    function invariant_no_purchase_exceeds_the_per_purchase_cap() public view {
        require(handler.oversizedSuccesses() == 0, "per-purchase cap exceeded");
    }

    function invariant_the_owner_never_loses_more_than_the_grant() public view {
        require(handler.spentTotal() <= GRANT, "more than the grant");
        require(token.balanceOf(OWNER) == BALANCE - handler.spentTotal(), "owner balance drifted");
    }

    function invariant_only_the_agent_moves_money() public view {
        require(handler.unauthorisedSuccesses() == 0, "a stranger spent");
    }

    function invariant_a_ref_pays_at_most_once() public view {
        require(handler.replaySuccesses() == 0, "a ref paid twice");
    }
}

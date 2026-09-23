// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

import {AgentAllowance, IERC20} from "../src/AgentAllowance.sol";
import {Asserts, FalseToken, MockUSDC, vm} from "./Support.sol";

/// Check T3 in docs/trezor-allowance-v1.md. The refusals are the specification:
/// a limiter that passes only the happy path has not been tested at all.
contract AgentAllowanceTest is Asserts {
    address constant OWNER = address(0xA11CE);
    address constant AGENT = address(0xA6E47);
    address constant GUARDIAN = address(0x6A4D);
    address constant PAYEE = address(0xB17);
    address constant STRANGER = address(0x5757);

    uint256 constant USDC = 1e6;
    uint256 constant DAILY = 100 * USDC;
    uint256 constant PER_PURCHASE = 10 * USDC;
    uint256 constant GRANT = 300 * USDC;
    uint256 constant BALANCE = 1_000 * USDC;
    // Noon UTC on day 20_000, so "today" is unambiguous.
    uint256 constant START = 20_000 days + 12 hours;

    MockUSDC token;
    AgentAllowance limiter;
    uint256 nextRef = 1;

    function setUp() public {
        vm.warp(START);
        token = new MockUSDC();
        token.mint(OWNER, BALANCE);
        limiter = _deploy(address(token), START + 30 days);
        vm.prank(OWNER);
        token.approve(address(limiter), GRANT);
    }

    function _deploy(address token_, uint256 expiry) internal returns (AgentAllowance) {
        return new AgentAllowance(IERC20(token_), OWNER, AGENT, GUARDIAN, DAILY, PER_PURCHASE, expiry);
    }

    function _ref() internal returns (bytes32) {
        return bytes32(nextRef++);
    }

    function _spend(uint256 amount) internal {
        vm.prank(AGENT);
        limiter.spend(PAYEE, amount, _ref());
    }

    // --- what it does ---

    function test_spend_moves_exact_amount_from_owner_to_payee() public {
        _spend(7 * USDC);
        assertEq(token.balanceOf(OWNER), BALANCE - 7 * USDC, "owner debited exactly");
        assertEq(token.balanceOf(PAYEE), 7 * USDC, "payee credited exactly");
        assertEq(limiter.allowanceLeft(), GRANT - 7 * USDC, "allowance consumed");
        assertEq(limiter.remainingToday(), DAILY - 7 * USDC, "daily budget consumed");
    }

    function test_approve_moves_nothing_and_limiter_never_holds_tokens() public {
        assertEq(token.balanceOf(OWNER), BALANCE, "approve moved nothing");
        assertEq(token.balanceOf(address(limiter)), 0, "empty after approve");
        _spend(10 * USDC);
        _spend(3 * USDC);
        assertEq(token.balanceOf(address(limiter)), 0, "empty after spends");
    }

    // --- who may spend ---

    function test_only_the_agent_can_spend() public {
        address[3] memory others = [OWNER, GUARDIAN, STRANGER];
        for (uint256 i; i < others.length; ++i) {
            vm.prank(others[i]);
            vm.expectRevert(AgentAllowance.NotAgent.selector);
            limiter.spend(PAYEE, USDC, _ref());
        }
        assertEq(token.balanceOf(OWNER), BALANCE, "nothing moved");
    }

    // --- the caps ---

    function test_per_purchase_cap_is_inclusive() public {
        _spend(PER_PURCHASE);
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.PerPurchaseCapExceeded.selector);
        limiter.spend(PAYEE, PER_PURCHASE + 1, _ref());
    }

    function test_daily_cap_allows_ten_purchases_and_refuses_the_eleventh() public {
        for (uint256 i; i < 10; ++i) _spend(PER_PURCHASE);
        assertEq(limiter.remainingToday(), 0, "day exhausted");
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.DailyCapExceeded.selector);
        limiter.spend(PAYEE, 1, _ref());
    }

    function test_daily_cap_resets_at_utc_midnight() public {
        for (uint256 i; i < 10; ++i) _spend(PER_PURCHASE);
        vm.warp((START / 1 days + 1) * 1 days); // 00:00:00 next day
        assertEq(limiter.remainingToday(), DAILY, "fresh day");
        _spend(PER_PURCHASE);
    }

    function test_midnight_burst_spends_two_caps_but_the_allowance_still_bounds_it() public {
        uint256 midnight = (START / 1 days + 1) * 1 days;
        vm.warp(midnight - 60); // 23:59
        for (uint256 i; i < 10; ++i) _spend(PER_PURCHASE);
        vm.warp(midnight + 60); // 00:01
        for (uint256 i; i < 10; ++i) _spend(PER_PURCHASE);
        assertEq(token.balanceOf(PAYEE), 2 * DAILY, "two caps in two minutes, as documented");

        vm.warp(midnight + 1 days);
        for (uint256 i; i < 10; ++i) _spend(PER_PURCHASE);
        assertEq(limiter.allowanceLeft(), 0, "grant used up");
        vm.prank(AGENT);
        vm.expectRevert(); // the token refuses: the allowance is the real ceiling
        limiter.spend(PAYEE, 1, _ref());
        assertEq(token.balanceOf(OWNER), BALANCE - GRANT, "never more than the grant");
    }

    // --- time and the kill switches ---

    function test_expiry_is_exclusive() public {
        vm.warp(START + 30 days - 1);
        _spend(USDC);
        vm.warp(START + 30 days);
        assertEq(limiter.remainingToday(), 0, "nothing left once expired");
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.Expired.selector);
        limiter.spend(PAYEE, USDC, _ref());
    }

    function test_guardian_pause_is_permanent() public {
        vm.prank(GUARDIAN);
        limiter.pause();
        assertEq(limiter.remainingToday(), 0, "nothing left once paused");
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.IsPaused.selector);
        limiter.spend(PAYEE, USDC, _ref());
        vm.warp(START + 2 days);
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.IsPaused.selector);
        limiter.spend(PAYEE, USDC, _ref());
    }

    function test_owner_can_pause_and_nobody_else_but_the_guardian() public {
        address[2] memory others = [AGENT, STRANGER];
        for (uint256 i; i < others.length; ++i) {
            vm.prank(others[i]);
            vm.expectRevert(AgentAllowance.NotGuardian.selector);
            limiter.pause();
        }
        vm.prank(OWNER);
        limiter.pause();
        assertTrue(limiter.paused(), "owner paused");
    }

    function test_revoking_the_allowance_stops_everything() public {
        _spend(USDC);
        vm.prank(OWNER);
        token.approve(address(limiter), 0);
        vm.prank(AGENT);
        vm.expectRevert();
        limiter.spend(PAYEE, USDC, _ref());
        assertEq(token.balanceOf(OWNER), BALANCE - USDC, "only the first purchase left");
    }

    // --- references ---

    function test_a_ref_pays_once() public {
        vm.prank(AGENT);
        limiter.spend(PAYEE, USDC, bytes32(uint256(42)));
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.RefUsed.selector);
        limiter.spend(PAYEE, USDC, bytes32(uint256(42)));
    }

    function test_zero_ref_is_refused() public {
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.InvalidRef.selector);
        limiter.spend(PAYEE, USDC, bytes32(0));
    }

    function test_a_failed_transfer_consumes_neither_budget_nor_ref() public {
        vm.prank(OWNER);
        token.approve(address(limiter), 0);
        bytes32 ref = bytes32(uint256(7));
        vm.prank(AGENT);
        vm.expectRevert();
        limiter.spend(PAYEE, USDC, ref);
        assertTrue(!limiter.usedRef(ref), "ref still free");
        assertEq(limiter.remainingToday(), DAILY, "budget untouched");

        vm.prank(OWNER);
        token.approve(address(limiter), GRANT);
        vm.prank(AGENT);
        limiter.spend(PAYEE, USDC, ref);
    }

    function test_insufficient_balance_reverts_without_debt() public {
        address poor = address(0x9002);
        AgentAllowance other = new AgentAllowance(
            IERC20(address(token)), poor, AGENT, GUARDIAN, DAILY, PER_PURCHASE, START + 1 days
        );
        token.mint(poor, 2 * USDC);
        vm.prank(poor);
        token.approve(address(other), GRANT);
        vm.prank(AGENT);
        vm.expectRevert();
        other.spend(PAYEE, 3 * USDC, _ref());
        assertEq(token.balanceOf(poor), 2 * USDC, "balance untouched");
    }

    // --- payees and amounts ---

    function test_invalid_payees_are_refused() public {
        address[3] memory bad = [address(0), address(limiter), OWNER];
        for (uint256 i; i < bad.length; ++i) {
            vm.prank(AGENT);
            vm.expectRevert(AgentAllowance.InvalidPayee.selector);
            limiter.spend(bad[i], USDC, _ref());
        }
    }

    function test_zero_amount_is_refused() public {
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.ZeroAmount.selector);
        limiter.spend(PAYEE, 0, _ref());
    }

    function test_a_token_returning_false_is_a_failure() public {
        AgentAllowance other = _deploy(address(new FalseToken()), START + 1 days);
        vm.prank(AGENT);
        vm.expectRevert(AgentAllowance.TransferFailed.selector);
        other.spend(PAYEE, USDC, _ref());
    }

    // --- construction ---

    function test_invalid_configurations_are_refused() public {
        IERC20 t = IERC20(address(token));
        uint256 later = START + 1 days;

        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(IERC20(address(0)), OWNER, AGENT, GUARDIAN, DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, address(0), AGENT, GUARDIAN, DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, address(0), GUARDIAN, DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, AGENT, address(0), DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, OWNER, GUARDIAN, DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, AGENT, OWNER, DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, AGENT, AGENT, DAILY, PER_PURCHASE, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, AGENT, GUARDIAN, DAILY, 0, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, AGENT, GUARDIAN, DAILY, DAILY + 1, later);
        vm.expectRevert(AgentAllowance.InvalidConfig.selector);
        new AgentAllowance(t, OWNER, AGENT, GUARDIAN, DAILY, PER_PURCHASE, START);
    }

    // --- fuzz ---

    function testFuzz_any_amount_is_either_within_the_caps_or_refused(uint256 amount) public {
        vm.prank(AGENT);
        if (amount == 0) {
            vm.expectRevert(AgentAllowance.ZeroAmount.selector);
            limiter.spend(PAYEE, amount, _ref());
        } else if (amount > PER_PURCHASE) {
            vm.expectRevert(AgentAllowance.PerPurchaseCapExceeded.selector);
            limiter.spend(PAYEE, amount, _ref());
        } else {
            limiter.spend(PAYEE, amount, _ref());
            assertEq(token.balanceOf(OWNER), BALANCE - amount, "exact debit");
        }
    }
}

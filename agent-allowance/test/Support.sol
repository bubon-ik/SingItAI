// SPDX-License-Identifier: MIT
pragma solidity 0.8.24;

/// The Foundry cheatcodes these tests use, declared here instead of pulling in
/// forge-std. The address is Foundry's fixed cheatcode address.
interface Vm {
    function prank(address) external;
    function warp(uint256) external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
    function assume(bool) external;
}

Vm constant vm = Vm(0x7109709ECfa91a80626fF3989D68f67F5b1DD12D);

abstract contract Asserts {
    function assertEq(uint256 a, uint256 b, string memory why) internal pure {
        require(a == b, why);
    }

    function assertTrue(bool ok, string memory why) internal pure {
        require(ok, why);
    }
}

/// Behaves like USDC where it matters: returns true or reverts, and consumes
/// allowance on transferFrom.
contract MockUSDC {
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;

    function mint(address to, uint256 amount) external {
        balanceOf[to] += amount;
    }

    function approve(address spender, uint256 amount) external returns (bool) {
        allowance[msg.sender][spender] = amount;
        return true;
    }

    function transferFrom(address from, address to, uint256 amount) external returns (bool) {
        uint256 allowed = allowance[from][msg.sender];
        require(allowed >= amount, "ERC20: insufficient allowance");
        require(balanceOf[from] >= amount, "ERC20: transfer amount exceeds balance");
        allowance[from][msg.sender] = allowed - amount;
        balanceOf[from] -= amount;
        balanceOf[to] += amount;
        return true;
    }
}

/// A token that reports failure instead of reverting.
contract FalseToken {
    function transferFrom(address, address, uint256) external pure returns (bool) {
        return false;
    }

    function allowance(address, address) external pure returns (uint256) {
        return type(uint256).max;
    }
}

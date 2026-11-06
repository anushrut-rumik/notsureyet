// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// Test-only principal accounting. Displayed RAY APR NEVER accrues interest.
contract MockPool is ReentrancyGuard {
    using SafeERC20 for IERC20;
    address public immutable owner;
    IERC20 public immutable asset;
    uint256 public currentLiquidityRate;
    uint256 public capacity;
    uint256 public totalDeposits;
    mapping(address => uint256) public balanceOf;
    error Invalid();
    error Unauthorized();

    constructor(address owner_, address asset_, uint256 rateRay, uint256 capacity_) {
        if (owner_ == address(0) || asset_.code.length == 0 || capacity_ == 0) revert Invalid();
        owner = owner_;
        asset = IERC20(asset_);
        _rate(rateRay);
        capacity = capacity_;
    }

    function _rate(uint256 rateRay) private {
        if (rateRay > 1_000_000 * 1e23 || rateRay % 1e23 != 0) revert Invalid();
        currentLiquidityRate = rateRay;
    }

    function setCurrentLiquidityRate(uint256 rateRay) external {
        if (msg.sender != owner) revert Unauthorized();
        _rate(rateRay);
    }

    function depositCapacity() public view returns (uint256) {
        return capacity - totalDeposits;
    }

    function supply(uint256 amount) external nonReentrant {
        if (amount == 0 || amount > depositCapacity()) revert Invalid();
        uint256 beforeBalance = asset.balanceOf(address(this));
        asset.safeTransferFrom(msg.sender, address(this), amount);
        if (asset.balanceOf(address(this)) - beforeBalance != amount) revert Invalid();
        balanceOf[msg.sender] += amount;
        totalDeposits += amount;
    }

    function withdraw(uint256 amount, address to) external nonReentrant {
        if (to == address(0) || amount == 0 || amount > balanceOf[msg.sender]) revert Invalid();
        balanceOf[msg.sender] -= amount;
        totalDeposits -= amount;
        asset.safeTransfer(to, amount);
    }
}

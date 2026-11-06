// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {MockPool} from "../../../src/lending/MockPool.sol";
import {ExecutionStrategyWallet} from "../../../src/lending/part3/ExecutionStrategyWallet.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";

interface Part3ScriptVm {
    function startBroadcast(address) external;
    function stopBroadcast() external;
}

/// USER-RUN ONLY. One chain per invocation, no forks, no implicit second-chain deployment.
contract DeployExecutionLending {
    Part3ScriptVm constant vm = Part3ScriptVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    struct Settings {
        address testOwner;
        bytes32 workflowId;
        bytes32 policyVersion;
        uint256 reserve;
        uint256 maxMove;
        uint256 rateRay;
        uint256 capacity;
    }

    function infrastructure() internal view returns (address token, address link, address router, address forwarder) {
        if (block.chainid == 11155111) {
            token = 0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05;
            link = 0x779877A7B0D9E8603169DdbD7836e478b4624789;
            router = 0x0BF3dE8c5D3e8A2B34D2BEeB17ABfCeBaf363A59;
            forwarder = 0x15fC6ae953E024d975e77382eEeC56A9101f9F88;
        } else if (block.chainid == 84532) {
            token = 0x88A2d74F47a237a62e7A51cdDa67270CE381555e;
            link = 0xE4aB69C077896252FAFBD49EFD26B5D171A32410;
            router = 0xD3b06cEbF099CE7DA4AcCf578aaebFDBd6e88a93;
            forwarder = 0x82300bd7c3958625581cc2F77bC6464dcEcDF3e5;
        } else {
            revert("Only Ethereum Sepolia and Base Sepolia");
        }
        require(
            token.code.length > 0 && link.code.length > 0 && router.code.length > 0 && forwarder.code.length > 0,
            "Missing infrastructure"
        );
    }

    function run(Settings memory s) external returns (MockPool pool, ExecutionStrategyWallet wallet) {
        require(s.testOwner != address(0) && s.testOwner.balance > 0, "Fund dedicated TEST signer gas first");
        (address token, address link, address router, address forwarder) = infrastructure();
        vm.startBroadcast(s.testOwner);
        pool = new MockPool(s.testOwner, token, s.rateRay, s.capacity);
        wallet = new ExecutionStrategyWallet(
            s.testOwner, pool, link, router, forwarder, s.workflowId, s.policyVersion, s.reserve, s.maxMove
        );
        vm.stopBroadcast();
    }

    /// Preferred Part 3 path: reuse the confirmed, owned MockPool and deploy only its new execution wallet.
    function runWallet(Settings memory s, MockPool existingPool) external returns (ExecutionStrategyWallet wallet) {
        require(s.testOwner != address(0) && s.testOwner.balance > 0, "Fund dedicated TEST signer gas first");
        (address token, address link, address router, address forwarder) = infrastructure();
        require(
            existingPool.owner() == s.testOwner && address(existingPool.asset()) == token,
            "Wrong existing owned test pool"
        );
        vm.startBroadcast(s.testOwner);
        wallet = new ExecutionStrategyWallet(
            s.testOwner, existingPool, link, router, forwarder, s.workflowId, s.policyVersion, s.reserve, s.maxMove
        );
        vm.stopBroadcast();
    }
}

contract ConfigureAndFundExecutionLending {
    Part3ScriptVm constant vm = Part3ScriptVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    /// Set the verified opposite-chain wallet, send exact BnM and LINK amounts,
    /// and explicitly supply principal. Use principal=0 on the empty destination.
    function run(ExecutionStrategyWallet wallet, address peer, uint256 principal, uint256 idle, uint256 linkAmount)
        external
    {
        require(block.chainid == 11155111 || block.chainid == 84532, "Testnets only");
        address owner = wallet.owner();
        require(idle >= wallet.protectedReserve(), "Reserve underfunded");
        require(
            wallet.pool().balanceOf(address(wallet)) == 0 && wallet.asset().balanceOf(address(wallet)) == 0
                && wallet.link().balanceOf(address(wallet)) == 0,
            "Already funded; reconcile first"
        );
        require(
            wallet.asset().balanceOf(owner) >= principal + idle && wallet.link().balanceOf(owner) >= linkAmount,
            "Faucet funds missing"
        );
        vm.startBroadcast(owner);
        wallet.setPeer(peer);
        require(wallet.asset().transfer(address(wallet), principal + idle), "BnM transfer failed");
        if (linkAmount > 0) require(wallet.link().transfer(address(wallet), linkAmount), "LINK transfer failed");
        if (principal > 0) wallet.depositToPool(principal);
        vm.stopBroadcast();
        require(
            wallet.pool().balanceOf(address(wallet)) == principal && wallet.asset().balanceOf(address(wallet)) == idle,
            "Funding postcondition"
        );
    }
}

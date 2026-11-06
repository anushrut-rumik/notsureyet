// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {CCIPSender} from "../src/CCIPSender.sol";
import {IRouterClient} from "@chainlink/contracts-ccip/contracts/interfaces/IRouterClient.sol";

interface DeployVm {
    function startBroadcast() external;
    function stopBroadcast() external;
}

contract DeployCCIPSender {
    DeployVm constant vm = DeployVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    address constant ROUTER = 0x0BF3dE8c5D3e8A2B34D2BEeB17ABfCeBaf363A59;
    address constant TOKEN = 0xFd57b4ddBf88a4e07fF4e34C487b99af2Fe82a05;
    uint64 constant DESTINATION = 10344971235874465080;

    function run() external returns (CCIPSender deployed) {
        require(block.chainid == 11155111, "Only deploy to Ethereum Sepolia");
        require(ROUTER.code.length > 0 && TOKEN.code.length > 0, "Missing official route contracts");
        require(IRouterClient(ROUTER).isChainSupported(DESTINATION), "Route not supported");
        vm.startBroadcast();
        deployed = new CCIPSender(ROUTER, TOKEN, DESTINATION);
        vm.stopBroadcast();
    }
}

// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {CCIPSender} from "../src/CCIPSender.sol";
import {CCIPLocalSimulator} from "@chainlink/local/src/ccip/CCIPLocalSimulator.sol";
import {CCIPLocalRouter} from "@chainlink/local/src/ccip/CCIPLocalRouter.sol";
import {IRouterClient} from "@chainlink/contracts-ccip/contracts/interfaces/IRouterClient.sol";
import {ExtraArgsCodec} from "@chainlink/contracts-ccip/contracts/libraries/ExtraArgsCodec.sol";
import {BurnMintERC677Helper} from "@chainlink/local/src/ccip/BurnMintERC677Helper.sol";

interface SimulationVm {
    function deal(address who, uint256 balance) external;
    function startPrank(address sender) external;
    function stopPrank() external;
}

/// @notice Isolated Foundry simulation. No RPC, fork, wallet key or broadcast is used.
contract SimulateCCIPTransfer {
    SimulationVm constant vm = SimulationVm(address(uint160(uint256(keccak256("hevm cheat code")))));

    struct Result {
        address senderContract;
        bytes32 messageId;
        uint256 nativeFee;
        uint256 recipientBefore;
        uint256 recipientAfter;
        uint64 simulatorSelector;
        bool duplicateRejected;
    }

    function run(address wallet, address recipient, uint256 amount, bytes32 intent)
        external
        returns (bytes memory report)
    {
        Result memory result;
        require(wallet != address(0) && recipient != address(0), "Invalid wallet or recipient");
        require(amount > 0 && amount <= 10 ether, "Simulation accepts up to 10 CCIP-BnM");
        CCIPLocalSimulator simulator = new CCIPLocalSimulator();
        IRouterClient router;
        BurnMintERC677Helper token;
        (result.simulatorSelector, router,,,, token,) = simulator.configuration();
        CCIPLocalRouter(address(router)).setFee(0.001 ether);
        CCIPSender sender = new CCIPSender(address(router), address(token), result.simulatorSelector);
        result.senderContract = address(sender);
        // Faucet balances exist only in this temporary EVM; the user's real wallet is untouched.
        for (uint256 i; i < (amount + 1 ether - 1) / 1 ether; ++i) {
            token.drip(wallet);
        }
        vm.deal(wallet, 1 ether);
        bytes memory extraArgs = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        result.nativeFee = sender.quote(recipient, amount, extraArgs);
        result.recipientBefore = token.balanceOf(recipient);
        uint256 sourceBefore = token.balanceOf(wallet);
        vm.startPrank(wallet);
        token.approve(result.senderContract, amount);
        result.messageId = sender.send{value: result.nativeFee}(intent, recipient, amount, result.nativeFee, extraArgs);
        try sender.send{value: result.nativeFee}(intent, recipient, amount, result.nativeFee, extraArgs) {
            revert("Duplicate send unexpectedly succeeded");
        } catch (bytes memory reason) {
            require(bytes4(reason) == CCIPSender.AlreadySent.selector, "Unexpected duplicate rejection");
            result.duplicateRejected = true;
        }
        vm.stopPrank();
        result.recipientAfter = token.balanceOf(recipient);
        uint256 selfTransfer = wallet == recipient ? amount : 0;
        require(result.recipientAfter == result.recipientBefore + amount - selfTransfer, "Delivery balance mismatch");
        require(token.balanceOf(wallet) == sourceBefore - amount + selfTransfer, "Source balance mismatch");
        require(
            sender.messageIds(wallet, intent) == result.messageId && result.messageId != bytes32(0),
            "Message not recorded"
        );
        require(wallet.balance == 1 ether - result.nativeFee, "Incorrect native fee");
        return abi.encode(result);
    }
}

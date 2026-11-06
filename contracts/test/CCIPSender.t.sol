// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {CCIPSender} from "../src/CCIPSender.sol";
import {CCIPLocalSimulator} from "@chainlink/local/src/ccip/CCIPLocalSimulator.sol";
import {BurnMintERC677Helper} from "@chainlink/local/src/ccip/BurnMintERC677Helper.sol";
import {IRouterClient} from "@chainlink/contracts-ccip/contracts/interfaces/IRouterClient.sol";
import {ExtraArgsCodec} from "@chainlink/contracts-ccip/contracts/libraries/ExtraArgsCodec.sol";
import {CCIPLocalRouter} from "@chainlink/local/src/ccip/CCIPLocalRouter.sol";

interface TestVm {
    function deal(address who, uint256 balance) external;
    function expectRevert() external;
    function expectRevert(bytes4 selector) external;
    function expectPartialRevert(bytes4 selector) external;
}

/// @dev Chainlink Local routes synchronously and uses a synthetic selector, not the live Base selector.
contract CCIPSenderTest {
    TestVm constant vm = TestVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    CCIPSender sender;
    BurnMintERC677Helper token;
    IRouterClient router;
    uint64 selector;
    address constant RECIPIENT = address(0xBEEF);
    bytes32 constant INTENT = keccak256("one durable intent");

    receive() external payable {}

    function setUp() public {
        CCIPLocalSimulator simulator = new CCIPLocalSimulator();
        (selector, router,,,, token,) = simulator.configuration();
        CCIPLocalRouter(address(router)).setFee(0.001 ether);
        sender = new CCIPSender(address(router), address(token), selector);
        token.drip(address(this));
        token.approve(address(sender), 1 ether);
        vm.deal(address(this), 10 ether);
    }

    function testNativeFeeTransferDeliversAndRecordsMessage() public {
        bytes memory extraArgs = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        uint256 fee = sender.quote(RECIPIENT, 1 ether, extraArgs);
        uint256 before = address(this).balance;
        bytes32 id = sender.send{value: fee + 1 ether}(INTENT, RECIPIENT, 1 ether, fee + 1 ether, extraArgs);
        require(id != bytes32(0), "Missing message ID");
        require(sender.messageIds(address(this), INTENT) == id, "Intent not recorded");
        require(token.balanceOf(RECIPIENT) == 1 ether, "Destination did not receive token");
        require(address(this).balance == before - fee, "Native surplus was not refunded");
        require(address(sender).balance == 0, "Native funds trapped");
        require(token.allowance(address(sender), address(router)) == 0, "Router allowance remains");
    }

    function testDuplicateIntentCannotSendTwice() public {
        bytes memory args = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        uint256 fee = sender.quote(RECIPIENT, 1 ether, args);
        sender.send{value: fee}(INTENT, RECIPIENT, 1 ether, fee, args);
        vm.expectPartialRevert(CCIPSender.AlreadySent.selector);
        sender.send{value: fee}(INTENT, RECIPIENT, 1 ether, fee, args);
        require(token.balanceOf(RECIPIENT) == 1 ether, "Duplicate delivered");
    }

    function testRejectsInvalidAmountsAndRecipients() public {
        vm.expectRevert(CCIPSender.InvalidTransfer.selector);
        sender.quote(address(0), 1 ether, "");
        vm.expectRevert(CCIPSender.InvalidTransfer.selector);
        sender.quote(RECIPIENT, 0, "");
        vm.expectRevert(CCIPSender.InvalidTransfer.selector);
        sender.send(bytes32(0), RECIPIENT, 1 ether, 0, "");
    }

    function testRequiresUserTokenAllowance() public {
        token.approve(address(sender), 0);
        bytes memory args = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        uint256 fee = sender.quote(RECIPIENT, 1 ether, args);
        vm.expectRevert();
        sender.send{value: fee}(INTENT, RECIPIENT, 1 ether, fee, args);
        require(sender.messageIds(address(this), INTENT) == bytes32(0), "Failed intent consumed");
    }

    function testWrongNativePaymentRevertsAtomically() public {
        bytes memory args = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        uint256 fee = sender.quote(RECIPIENT, 1 ether, args);
        vm.expectPartialRevert(CCIPSender.FeeExceedsLimit.selector);
        sender.send{value: fee + 1}(INTENT, RECIPIENT, 1 ether, fee, args);
        require(sender.messageIds(address(this), INTENT) == bytes32(0), "Failed intent consumed");
    }

    function testFeeRiseCannotExceedReviewedCap() public {
        bytes memory args = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        uint256 fee = sender.quote(RECIPIENT, 1 ether, args);
        uint256 balanceBefore = token.balanceOf(address(this));
        CCIPLocalRouter(address(router)).setFee(fee * 2);
        vm.expectPartialRevert(CCIPSender.FeeExceedsLimit.selector);
        sender.send{value: fee}(INTENT, RECIPIENT, 1 ether, fee, args);
        require(token.balanceOf(address(this)) == balanceBefore, "Tokens moved above fee cap");
        require(token.balanceOf(RECIPIENT) == 0, "Unexpected delivery");
        require(sender.messageIds(address(this), INTENT) == bytes32(0), "Failed intent consumed");
    }

    function testV3EncodingMatchesPythonDefault() public pure {
        bytes memory args = ExtraArgsCodec._getBasicEncodedExtraArgsV3(0, bytes4(0));
        require(keccak256(args) == keccak256(hex"a69dd4aa000000000000000000000000000000"), "Wrong V3 encoding");
    }
}

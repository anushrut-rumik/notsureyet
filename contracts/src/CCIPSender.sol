// SPDX-License-Identifier: MIT
pragma solidity ^0.8.26;

import {IRouterClient} from "@chainlink/contracts-ccip/contracts/interfaces/IRouterClient.sol";
import {Client} from "@chainlink/contracts-ccip/contracts/libraries/Client.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";

/// @notice One token and one destination per deployment. Users retain their tokens until sending.
contract CCIPSender is ReentrancyGuard {
    using SafeERC20 for IERC20;
    IRouterClient public immutable router;
    IERC20 public immutable token;
    uint64 public immutable destination;
    mapping(address => mapping(bytes32 => bytes32)) public messageIds;

    event TransferSent(
        bytes32 indexed intentId,
        bytes32 indexed messageId,
        address indexed sender,
        address recipient,
        uint256 amount,
        uint256 fee
    );
    error InvalidTransfer();
    error AlreadySent(bytes32 messageId);
    error FeeExceedsLimit(uint256 fee, uint256 limit);
    error RefundFailed();

    constructor(address router_, address token_, uint64 destination_) {
        if (router_.code.length == 0 || token_.code.length == 0 || destination_ == 0) revert InvalidTransfer();
        router = IRouterClient(router_);
        token = IERC20(token_);
        destination = destination_;
    }

    function quote(address recipient, uint256 amount, bytes calldata extraArgs) external view returns (uint256) {
        return router.getFee(destination, _message(recipient, amount, extraArgs));
    }

    /// @param intentId Stable per-wallet intent, retained on retries. A second send reverts atomically.
    /// @param maxFee Maximum native CCIP fee explicitly reviewed by the user; surplus ETH is refunded.
    function send(bytes32 intentId, address recipient, uint256 amount, uint256 maxFee, bytes calldata extraArgs)
        external
        payable
        nonReentrant
        returns (bytes32 messageId)
    {
        if (intentId == bytes32(0)) revert InvalidTransfer();
        if (messageIds[msg.sender][intentId] != bytes32(0)) revert AlreadySent(messageIds[msg.sender][intentId]);
        Client.EVM2AnyMessage memory message = _message(recipient, amount, extraArgs);
        uint256 fee = router.getFee(destination, message);
        if (fee > maxFee || msg.value != maxFee) revert FeeExceedsLimit(fee, maxFee);
        token.safeTransferFrom(msg.sender, address(this), amount);
        token.forceApprove(address(router), amount);
        messageId = router.ccipSend{value: fee}(destination, message);
        messageIds[msg.sender][intentId] = messageId;
        token.forceApprove(address(router), 0);
        emit TransferSent(intentId, messageId, msg.sender, recipient, amount, fee);
        if (msg.value > fee) {
            (bool refunded,) = msg.sender.call{value: msg.value - fee}("");
            if (!refunded) revert RefundFailed();
        }
    }

    function _message(address recipient, uint256 amount, bytes calldata extraArgs)
        private
        view
        returns (Client.EVM2AnyMessage memory)
    {
        if (recipient == address(0) || amount == 0 || !router.isChainSupported(destination)) {
            revert InvalidTransfer();
        }
        Client.EVMTokenAmount[] memory amounts = new Client.EVMTokenAmount[](1);
        amounts[0] = Client.EVMTokenAmount(address(token), amount);
        return Client.EVM2AnyMessage({
            receiver: abi.encode(recipient), data: "", tokenAmounts: amounts, feeToken: address(0), extraArgs: extraArgs
        });
    }
}

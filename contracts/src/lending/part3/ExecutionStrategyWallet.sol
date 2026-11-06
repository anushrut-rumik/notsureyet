// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;

import {MockPool} from "../MockPool.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";
import {CCIPReceiver} from "@chainlink/contracts-ccip/contracts/applications/CCIPReceiver.sol";
import {IRouterClient} from "@chainlink/contracts-ccip/contracts/interfaces/IRouterClient.sol";
import {Client} from "@chainlink/contracts-ccip/contracts/libraries/Client.sol";
import {ExtraArgsCodec} from "@chainlink/contracts-ccip/contracts/libraries/ExtraArgsCodec.sol";

/// Separate Part 3 deployment: Part 2 wallets remain read-only consumers.
/// Dedicated testnet principal only; this contract never accrues interest.
contract ExecutionStrategyWallet is EIP712, ReentrancyGuard, CCIPReceiver {
    using SafeERC20 for IERC20;

    struct Instruction {
        bytes32 operationId;
        uint256 sourceChainId;
        address consumer;
        uint64 destinationSelector;
        address destination;
        address token;
        uint256 amount;
        bytes32 policyVersion;
        uint256 expiry;
        uint256 maxLinkFee;
        uint256 maxGasPrice;
        uint256 sourceRateRay;
        uint256 observedAt;
        bytes32 inputHash;
    }
    bytes32 public constant INSTRUCTION_TYPEHASH = keccak256(
        "Instruction(bytes32 operationId,uint256 sourceChainId,address consumer,uint64 destinationSelector,address destination,address token,uint256 amount,bytes32 policyVersion,uint256 expiry,uint256 maxLinkFee,uint256 maxGasPrice,uint256 sourceRateRay,uint256 observedAt,bytes32 inputHash)"
    );
    address public immutable owner;
    MockPool public immutable pool;
    IERC20 public immutable asset;
    IERC20 public immutable link;
    IRouterClient public immutable router;
    address public immutable forwarder;
    bytes32 public immutable workflowId;
    // First ten ASCII hex characters of sha256("lending-p3"), per the CRE metadata encoding.
    bytes10 public constant WORKFLOW_NAME = bytes10("eb6597b43f");
    bool public immutable simulationForwarder;
    uint64 public immutable destinationSelector;
    uint256 public immutable protectedReserve;
    uint256 public immutable maxMove;
    bytes32 public policyVersion;
    address public peer;
    bytes32 public activeOperation;
    mapping(bytes32 => bytes32) public sentMessages;
    mapping(bytes32 => bool) public receivedOperations;
    enum DepositState {
        UNKNOWN,
        IDLE,
        DEPOSITED,
        RECOVERED
    }

    struct Deposit {
        Client.Any2EVMMessage message;
        bytes reason;
        DepositState state;
        bytes32 operationId;
        uint256 amount;
    }
    mapping(bytes32 => Deposit) private deposits;
    uint256 public reservedInbound;
    event SourceSubmitted(
        bytes32 indexed operationId,
        bytes32 indexed messageId,
        address destination,
        address token,
        uint256 amount,
        uint256 linkFee
    );
    event DepositFailed(bytes32 indexed messageId, bytes32 indexed operationId, uint256 amount, bytes reason);
    event PoolDeposited(
        bytes32 indexed messageId, bytes32 indexed operationId, address token, uint256 amount, uint256 principalAfter
    );
    event DepositRecovered(bytes32 indexed messageId);
    event OperationSettled(bytes32 indexed operationId, bytes32 indexed messageId);
    error Unauthorized();
    error InvalidInstruction();
    modifier onlyOwner() {
        if (msg.sender != owner) revert Unauthorized();
        _;
    }

    constructor(
        address owner_,
        MockPool pool_,
        address link_,
        address router_,
        address forwarder_,
        bytes32 workflowId_,
        bytes32 version_,
        uint256 reserve_,
        uint256 maxMove_
    ) EIP712("LendingExecutionInstruction", "1") CCIPReceiver(router_) {
        if (block.chainid != 11155111 && block.chainid != 84532) revert InvalidInstruction();
        if (
            owner_ == address(0) || address(pool_).code.length == 0 || link_.code.length == 0
                || router_.code.length == 0 || forwarder_ == address(0) || workflowId_ == 0 || version_ == 0
                || maxMove_ == 0 || pool_.owner() != owner_ || address(pool_.asset()) == link_
        ) revert InvalidInstruction();
        owner = owner_;
        pool = pool_;
        asset = pool_.asset();
        link = IERC20(link_);
        router = IRouterClient(router_);
        forwarder = forwarder_;
        workflowId = workflowId_;
        simulationForwarder = forwarder_
            == (block.chainid == 11155111
                    ? 0x15fC6ae953E024d975e77382eEeC56A9101f9F88
                    : 0x82300bd7c3958625581cc2F77bC6464dcEcDF3e5);
        policyVersion = version_;
        protectedReserve = reserve_;
        maxMove = maxMove_;
        destinationSelector = block.chainid == 11155111 ? 10344971235874465080 : 16015286601757825753;
    }

    // One permanent, pair-bound peer. Reconfiguration requires a new reviewed deployment.
    function setPeer(address peer_) external onlyOwner {
        if (peer != address(0) || peer_ == address(0)) revert InvalidInstruction();
        peer = peer_;
    }

    function setPolicyVersion(bytes32 version_) external onlyOwner {
        if (version_ == 0 || activeOperation != 0) revert InvalidInstruction();
        policyVersion = version_;
    }

    function depositToPool(uint256 amount) external onlyOwner nonReentrant {
        uint256 idle = asset.balanceOf(address(this));
        if (activeOperation != 0 || amount == 0 || idle < amount || idle - amount < protectedReserve + reservedInbound)
        {
            revert InvalidInstruction();
        }
        asset.forceApprove(address(pool), amount);
        pool.supply(amount);
        asset.forceApprove(address(pool), 0);
    }

    function withdrawIdle(uint256 amount) external onlyOwner nonReentrant {
        uint256 idle = asset.balanceOf(address(this));
        if (amount == 0 || idle < amount || idle - amount < protectedReserve + reservedInbound) {
            revert InvalidInstruction();
        }
        asset.safeTransfer(owner, amount);
    }

    function instructionDigest(Instruction memory i) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(INSTRUCTION_TYPEHASH, i)));
    }

    function consumed(bytes32 operationId) external view returns (bool) {
        return sentMessages[operationId] != 0;
    }

    function _message(bytes32 operationId, uint256 amount) internal view returns (Client.EVM2AnyMessage memory) {
        if (peer == address(0) || operationId == 0 || amount == 0 || !router.isChainSupported(destinationSelector)) {
            revert InvalidInstruction();
        }
        Client.EVMTokenAmount[] memory tokens = new Client.EVMTokenAmount[](1);
        tokens[0] = Client.EVMTokenAmount(address(asset), amount);
        return Client.EVM2AnyMessage({
            receiver: abi.encode(peer),
            data: abi.encode(operationId, amount),
            tokenAmounts: tokens,
            feeToken: address(link),
            extraArgs: ExtraArgsCodec._getBasicEncodedExtraArgsV3BlockDepth(500_000, 0)
        });
    }

    function quoteOperation(bytes32 operationId, uint256 amount) public view returns (uint256) {
        return router.getFee(destinationSelector, _message(operationId, amount));
    }

    // Compatibility with Part 2 observations; execution always checks quoteOperation for the actual ID.
    function quoteLink(uint256 amount) external view returns (uint256) {
        return quoteOperation(bytes32(uint256(1)), amount);
    }

    function onReport(bytes calldata metadata, bytes calldata report) external nonReentrant {
        if (msg.sender != forwarder) revert Unauthorized();
        if (simulationForwarder) {
            // Official MockKeystoneForwarder has no authenticated workflow metadata.
            // Never treat mock fields as identity. Owner EIP-712 signature below is mandatory.
            if (metadata.length != 0) revert Unauthorized();
        } else {
            if (metadata.length != 62 && metadata.length != 64) revert Unauthorized();
            if (
                bytes32(metadata[:32]) != workflowId || bytes10(metadata[32:42]) != WORKFLOW_NAME
                    || address(bytes20(metadata[42:62])) != owner
            ) revert Unauthorized();
        }
        (Instruction memory i, bytes memory signature) = abi.decode(report, (Instruction, bytes));
        if (keccak256(report) != keccak256(abi.encode(i, signature))) revert InvalidInstruction();
        if (
            i.operationId == 0 || activeOperation != 0 || sentMessages[i.operationId] != 0
                || i.sourceChainId != block.chainid || i.consumer != address(this)
                || i.destinationSelector != destinationSelector || i.destination != peer || i.token != address(asset)
                || i.amount == 0 || i.amount > maxMove || i.amount != pool.balanceOf(address(this))
                || i.policyVersion != policyVersion || block.timestamp >= i.expiry || i.observedAt > block.timestamp
                || block.timestamp - i.observedAt > 1800 || i.sourceRateRay != pool.currentLiquidityRate()
                || i.inputHash == 0 || asset.balanceOf(address(this)) < protectedReserve + reservedInbound
        ) revert InvalidInstruction();
        if (ECDSA.recover(instructionDigest(i), signature) != owner) revert Unauthorized();
        Client.EVM2AnyMessage memory message = _message(i.operationId, i.amount);
        uint256 fee = router.getFee(destinationSelector, message);
        if (fee > i.maxLinkFee || link.balanceOf(address(this)) < fee || tx.gasprice > i.maxGasPrice) {
            revert InvalidInstruction();
        }
        activeOperation = i.operationId;
        pool.withdraw(i.amount, address(this));
        asset.forceApprove(address(router), i.amount);
        link.forceApprove(address(router), fee);
        bytes32 messageId = router.ccipSend(destinationSelector, message);
        if (messageId == 0) revert InvalidInstruction();
        sentMessages[i.operationId] = messageId;
        asset.forceApprove(address(router), 0);
        link.forceApprove(address(router), 0);
        emit SourceSubmitted(i.operationId, messageId, peer, address(asset), i.amount, fee);
    }

    // Router authentication is inherited from CCIPReceiver. Pair and all token entries are checked here.
    function _ccipReceive(Client.Any2EVMMessage memory message) internal override nonReentrant {
        if (
            peer == address(0) || message.sourceChainSelector != destinationSelector || message.sender.length != 32
                || keccak256(message.sender) != keccak256(abi.encode(peer)) || message.messageId == 0
                || deposits[message.messageId].state != DepositState.UNKNOWN || message.destTokenAmounts.length == 0
        ) {
            revert InvalidInstruction();
        }
        (bytes32 operationId, uint256 amount) = abi.decode(message.data, (bytes32, uint256));
        if (
            keccak256(message.data) != keccak256(abi.encode(operationId, amount)) || operationId == 0
                || receivedOperations[operationId] || amount == 0 || amount > maxMove
        ) revert InvalidInstruction();
        uint256 total;
        for (uint256 n; n < message.destTokenAmounts.length; n++) {
            Client.EVMTokenAmount memory t = message.destTokenAmounts[n];
            if (t.token != address(asset) || t.amount == 0) revert InvalidInstruction();
            total += t.amount;
        }
        if (total != amount || asset.balanceOf(address(this)) < amount + reservedInbound) revert InvalidInstruction();
        receivedOperations[operationId] = true;
        Deposit storage d = deposits[message.messageId];
        d.message.messageId = message.messageId;
        d.message.sourceChainSelector = message.sourceChainSelector;
        d.message.sender = message.sender;
        d.message.data = message.data;
        for (uint256 n; n < message.destTokenAmounts.length; n++) {
            d.message.destTokenAmounts.push(message.destTokenAmounts[n]);
        }
        d.operationId = operationId;
        d.amount = amount;
        d.state = DepositState.IDLE;
        reservedInbound += amount;
        _tryDeposit(message.messageId);
    }

    function processDeposit(bytes32 messageId) external {
        if (msg.sender != address(this)) revert Unauthorized();
        Deposit storage d = deposits[messageId];
        uint256 beforePrincipal = pool.balanceOf(address(this));
        asset.forceApprove(address(pool), d.amount);
        pool.supply(d.amount);
        asset.forceApprove(address(pool), 0);
        if (pool.balanceOf(address(this)) != beforePrincipal + d.amount) revert InvalidInstruction();
    }

    function _tryDeposit(bytes32 messageId) private {
        Deposit storage d = deposits[messageId];
        try this.processDeposit(messageId) {
            reservedInbound -= d.amount;
            d.state = DepositState.DEPOSITED;
            emit PoolDeposited(messageId, d.operationId, address(asset), d.amount, pool.balanceOf(address(this)));
        } catch (bytes memory reason) {
            d.reason = reason;
            emit DepositFailed(messageId, d.operationId, d.amount, reason);
        }
    }

    function retryDeposit(bytes32 messageId) external onlyOwner nonReentrant {
        if (deposits[messageId].state != DepositState.IDLE) revert InvalidInstruction();
        _tryDeposit(messageId);
    }

    // Fixed owner recovery accounts for every original entry. Never an arbitrary recipient.
    function recoverFailedDeposit(bytes32 messageId) external onlyOwner nonReentrant {
        Deposit storage d = deposits[messageId];
        if (d.state != DepositState.IDLE) revert InvalidInstruction();
        d.state = DepositState.RECOVERED;
        reservedInbound -= d.amount;
        for (uint256 n; n < d.message.destTokenAmounts.length; n++) {
            Client.EVMTokenAmount storage t = d.message.destTokenAmounts[n];
            asset.safeTransfer(owner, t.amount);
        }
        emit DepositRecovered(messageId);
    }

    function getDeposit(bytes32 messageId) external view returns (Deposit memory) {
        return deposits[messageId];
    }

    // Owner acknowledgement is NOT destination proof. Offchain monitoring independently verifies it.
    function settleOperation(bytes32 operationId, bytes32 messageId) external onlyOwner {
        if (operationId == 0 || activeOperation != operationId || sentMessages[operationId] != messageId) {
            revert InvalidInstruction();
        }
        activeOperation = 0;
        emit OperationSettled(operationId, messageId);
    }

    function binding() external view returns (bytes32) {
        return keccak256(
            abi.encode(
                owner,
                address(pool),
                address(asset),
                address(link),
                address(router),
                forwarder,
                workflowId,
                policyVersion,
                protectedReserve,
                maxMove,
                peer
            )
        );
    }

    function observe() external view returns (uint256[12] memory v) {
        v = [
            block.chainid,
            block.number,
            block.timestamp,
            pool.currentLiquidityRate(),
            pool.balanceOf(address(this)),
            asset.balanceOf(address(this)),
            asset.balanceOf(address(pool)),
            pool.depositCapacity(),
            link.balanceOf(address(this)),
            owner.balance,
            address(this).balance,
            uint256(uint160(owner))
        ];
    }

    function supportsInterface(bytes4 id) public pure override returns (bool) {
        return id == bytes4(keccak256("onReport(bytes,bytes)")) || super.supportsInterface(id);
    }
}

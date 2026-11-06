// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {MockPool} from "./MockPool.sol";
import {IERC20} from "@openzeppelin/contracts/token/ERC20/IERC20.sol";
import {SafeERC20} from "@openzeppelin/contracts/token/ERC20/utils/SafeERC20.sol";
import {ReentrancyGuard} from "@openzeppelin/contracts/utils/ReentrancyGuard.sol";
import {EIP712} from "@openzeppelin/contracts/utils/cryptography/EIP712.sol";
import {ECDSA} from "@openzeppelin/contracts/utils/cryptography/ECDSA.sol";

// ABI-compatible CRE consumer interface; avoids an unrelated upstream OZ 5.0.2 dependency.
interface IReceiver {
    function onReport(bytes calldata metadata, bytes calldata report) external;
}
import {IRouterClient} from "@chainlink/contracts-ccip/contracts/interfaces/IRouterClient.sol";
import {Client} from "@chainlink/contracts-ccip/contracts/libraries/Client.sol";
import {ExtraArgsCodec} from "@chainlink/contracts-ccip/contracts/libraries/ExtraArgsCodec.sol";

/// Dedicated one-user, one-chain test wallet. Part 2 admits signed instructions
/// for regression testing, but NEVER sends CCIP messages or executes a rebalance.
contract TestStrategyWallet is EIP712, ReentrancyGuard, IReceiver {
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
    }
    bytes32 public constant INSTRUCTION_TYPEHASH = keccak256(
        "Instruction(bytes32 operationId,uint256 sourceChainId,address consumer,uint64 destinationSelector,address destination,address token,uint256 amount,bytes32 policyVersion,uint256 expiry)"
    );
    address public immutable owner;
    MockPool public immutable pool;
    IERC20 public immutable asset;
    IERC20 public immutable link;
    IRouterClient public immutable router;
    address public immutable forwarder;
    bytes32 public immutable workflowId;
    bytes10 public constant WORKFLOW_NAME = bytes10("lending-p2");
    uint64 public immutable destinationSelector;
    uint256 public immutable protectedReserve;
    uint256 public immutable maxMove;
    bytes32 public policyVersion;
    address public peer;
    mapping(bytes32 => bool) public consumed;
    event InstructionAdmitted(bytes32 indexed operationId, bytes32 digest);
    error Unauthorized();
    error InvalidInstruction();
    error Part3Disabled();
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
    ) EIP712("LendingTestInstruction", "1") {
        if (block.chainid != 11155111 && block.chainid != 84532) revert InvalidInstruction();
        if (
            owner_ == address(0) || address(pool_).code.length == 0 || link_.code.length == 0
                || router_.code.length == 0 || forwarder_ == address(0) || workflowId_ == 0 || version_ == 0
                || maxMove_ == 0
        ) {
            revert InvalidInstruction();
        }
        if (pool_.owner() != owner_ || address(pool_.asset()) == link_) revert InvalidInstruction();
        owner = owner_;
        pool = pool_;
        asset = pool_.asset();
        link = IERC20(link_);
        router = IRouterClient(router_);
        forwarder = forwarder_;
        workflowId = workflowId_;
        policyVersion = version_;
        protectedReserve = reserve_;
        maxMove = maxMove_;
        destinationSelector = block.chainid == 11155111 ? 10344971235874465080 : 16015286601757825753;
    }

    // Peer is explicitly bound to this user's opposite-chain wallet. No global sender allowlist.
    // There is deliberately no CCIP receiver in Part 2; every inbound CCIP call fails closed.
    function setPeer(address peer_) external onlyOwner {
        if (peer_ == address(0)) revert InvalidInstruction();
        peer = peer_;
    }

    function setPolicyVersion(bytes32 version_) external onlyOwner {
        if (version_ == 0) revert InvalidInstruction();
        policyVersion = version_;
    }

    function depositToPool(uint256 amount) external onlyOwner nonReentrant {
        uint256 idle = asset.balanceOf(address(this));
        if (amount == 0 || idle < amount || idle - amount < protectedReserve) revert InvalidInstruction();
        asset.forceApprove(address(pool), amount);
        pool.supply(amount);
        asset.forceApprove(address(pool), 0);
    }

    function withdrawFromPool(uint256 amount) external onlyOwner nonReentrant {
        pool.withdraw(amount, address(this));
    }

    function withdrawIdle(uint256 amount) external onlyOwner nonReentrant {
        uint256 idle = asset.balanceOf(address(this));
        if (amount == 0 || idle < amount || idle - amount < protectedReserve) revert InvalidInstruction();
        asset.safeTransfer(owner, amount);
    }

    function recoverLink(uint256 amount) external onlyOwner nonReentrant {
        if (amount == 0) revert InvalidInstruction();
        link.safeTransfer(owner, amount);
    }

    function instructionDigest(Instruction memory i) public view returns (bytes32) {
        return _hashTypedDataV4(keccak256(abi.encode(INSTRUCTION_TYPEHASH, i)));
    }

    function onReport(bytes calldata metadata, bytes calldata report) external nonReentrant {
        if (msg.sender != forwarder || (metadata.length != 62 && metadata.length != 64)) revert Unauthorized();
        if (
            bytes32(metadata[:32]) != workflowId || bytes10(metadata[32:42]) != WORKFLOW_NAME
                || address(bytes20(metadata[42:62])) != owner
        ) revert Unauthorized();
        (Instruction memory i, bytes memory signature) = abi.decode(report, (Instruction, bytes));
        // Reject function calldata, trailing bytes, alternate offsets and noncanonical tuples.
        if (keccak256(report) != keccak256(abi.encode(i, signature))) revert InvalidInstruction();
        if (
            i.operationId == 0 || consumed[i.operationId] || i.sourceChainId != block.chainid
                || i.consumer != address(this) || i.destinationSelector != destinationSelector || peer == address(0)
                || i.destination != peer || i.token != address(asset) || i.amount == 0 || i.amount > maxMove
                || i.amount > pool.balanceOf(address(this)) || i.policyVersion != policyVersion
                || block.timestamp >= i.expiry || asset.balanceOf(address(this)) < protectedReserve
        ) revert InvalidInstruction();
        bytes32 digest = instructionDigest(i);
        if (ECDSA.recover(digest, signature) != owner) revert Unauthorized();
        consumed[i.operationId] = true;
        emit InstructionAdmitted(i.operationId, digest);
        // Admission is NOT execution. No withdrawal/approval/send occurs here.
    }

    function withdrawFromPoolAndDepositCrossChain(Instruction calldata) external pure {
        revert Part3Disabled();
    }

    function supportsInterface(bytes4 id) external pure returns (bool) {
        return id == type(IReceiver).interfaceId || id == 0x01ffc9a7;
    }

    function quoteLink(uint256 amount) external view returns (uint256) {
        if (peer == address(0) || amount == 0 || !router.isChainSupported(destinationSelector)) {
            revert InvalidInstruction();
        }
        Client.EVMTokenAmount[] memory tokens = new Client.EVMTokenAmount[](1);
        tokens[0] = Client.EVMTokenAmount(address(asset), amount);
        return router.getFee(
            destinationSelector,
            Client.EVM2AnyMessage({
                receiver: abi.encode(peer),
                data: abi.encode(amount),
                tokenAmounts: tokens,
                feeToken: address(link),
                extraArgs: ExtraArgsCodec._getBasicEncodedExtraArgsV3BlockDepth(300_000, 0)
            })
        );
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

    // One atomic eth_call gives the CRE adapter consistent principal/rate/funding evidence.
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
}

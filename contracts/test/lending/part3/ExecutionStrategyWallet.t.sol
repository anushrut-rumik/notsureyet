// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {ExecutionStrategyWallet as Wallet} from "../../../src/lending/part3/ExecutionStrategyWallet.sol";
import {MockPool} from "../../../src/lending/MockPool.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";
import {Client} from "@chainlink/contracts-ccip/contracts/libraries/Client.sol";

interface VmP3 {
    function chainId(uint256) external;
    function warp(uint256) external;
    function addr(uint256) external returns (address);
    function sign(uint256, bytes32) external returns (uint8, bytes32, bytes32);
    function prank(address) external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
    function etch(address, bytes calldata) external;
    function txGasPrice(uint256) external;
}

contract P3Token is ERC20 {
    constructor() ERC20("Fault injection token", "MOCK") {}

    function mint(address a, uint256 n) external {
        _mint(a, n);
    }
}

// Local fault-injection router. These tests do NOT reproduce live CCIP verification/delivery.
contract P3Router {
    uint256 public fee = 1 ether;
    uint256 public sends;
    bool public supported = true;
    bytes public lastData;

    function setSupported(bool v) external {
        supported = v;
    }

    function setFee(uint256 v) external {
        fee = v;
    }

    function isChainSupported(uint64) external view returns (bool) {
        return supported;
    }

    function getFee(uint64, Client.EVM2AnyMessage memory) external view returns (uint256) {
        require(supported);
        return fee;
    }

    function ccipSend(uint64, Client.EVM2AnyMessage calldata m) external returns (bytes32) {
        for (uint256 n; n < m.tokenAmounts.length; n++) {
            ERC20(m.tokenAmounts[n].token).transferFrom(msg.sender, address(this), m.tokenAmounts[n].amount);
        }
        ERC20(m.feeToken).transferFrom(msg.sender, address(this), fee);
        lastData = m.data;
        sends++;
        return bytes32(sends);
    }

    function deliver(Wallet wallet, Client.Any2EVMMessage memory m) external {
        for (uint256 n; n < m.destTokenAmounts.length; n++) {
            ERC20(m.destTokenAmounts[n].token).transfer(address(wallet), m.destTokenAmounts[n].amount);
        }
        wallet.ccipReceive(m);
    }
}

contract P3Forwarder {
    function deliver(Wallet w, bytes memory meta, bytes memory report) external {
        w.onReport(meta, report);
    }

    function catchFailure(Wallet w, bytes memory meta, bytes memory report) external returns (bool ok) {
        (ok,) = address(w).call(abi.encodeCall(w.onReport, (meta, report)));
    }
}

contract ExecutionStrategyWalletTest {
    VmP3 constant vm = VmP3(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 constant TEST_SIGNER = 0x12345;
    bytes32 constant POLICY = keccak256("approved-p3-policy");
    bytes32 constant WORKFLOW = keccak256("workflow");
    address owner;
    P3Token token;
    P3Token link;
    P3Router router;
    P3Forwarder forwarder;
    MockPool pool;
    MockPool destinationPool;
    Wallet source;
    Wallet destination;

    function setUp() public {
        vm.warp(1000);
        vm.chainId(11155111);
        owner = vm.addr(TEST_SIGNER);
        token = new P3Token();
        link = new P3Token();
        router = new P3Router();
        forwarder = new P3Forwarder();
        pool = new MockPool(owner, address(token), 4e25, 1000 ether);
        source = new Wallet(
            owner, pool, address(link), address(router), address(forwarder), WORKFLOW, POLICY, 2 ether, 8 ether
        );
        vm.chainId(84532);
        destinationPool = new MockPool(owner, address(token), 7e25, 1000 ether);
        destination = new Wallet(
            owner,
            destinationPool,
            address(link),
            address(router),
            address(forwarder),
            WORKFLOW,
            POLICY,
            2 ether,
            8 ether
        );
        vm.prank(owner);
        destination.setPeer(address(source));
        token.mint(address(destination), 2 ether);
        vm.chainId(11155111);
        vm.prank(owner);
        source.setPeer(address(destination));
        token.mint(address(source), 10 ether);
        link.mint(address(source), 10 ether);
        vm.prank(owner);
        source.depositToPool(8 ether);
    }

    function instruction() internal view returns (Wallet.Instruction memory) {
        return Wallet.Instruction(
            keccak256("operation"),
            11155111,
            address(source),
            10344971235874465080,
            address(destination),
            address(token),
            8 ether,
            POLICY,
            1800,
            1 ether,
            1 gwei,
            4e25,
            1000,
            keccak256("immutable input")
        );
    }

    function metadata() internal view returns (bytes memory) {
        return abi.encodePacked(WORKFLOW, bytes10("eb6597b43f"), owner);
    }

    function signed(Wallet.Instruction memory i) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(TEST_SIGNER, source.instructionDigest(i));
        return abi.encode(i, abi.encodePacked(r, s, v));
    }

    function send() internal {
        forwarder.deliver(source, metadata(), signed(instruction()));
    }

    function rejected(Wallet.Instruction memory i) internal {
        bytes memory r = signed(i);
        vm.expectRevert();
        forwarder.deliver(source, metadata(), r);
    }

    function message() internal view returns (Client.Any2EVMMessage memory m) {
        m.messageId = bytes32(uint256(1));
        m.sourceChainSelector = 16015286601757825753;
        m.sender = abi.encode(address(source));
        m.data = abi.encode(instruction().operationId, 8 ether);
        m.destTokenAmounts = new Client.EVMTokenAmount[](1);
        m.destTokenAmounts[0] = Client.EVMTokenAmount(address(token), 8 ether);
    }

    function testHappyPathExactFeeReservesAndVerifiedPosition() public {
        send();
        require(
            router.sends() == 1 && pool.balanceOf(address(source)) == 0 && token.balanceOf(address(source)) == 2 ether
        );
        require(
            link.balanceOf(address(source)) == 9 ether && token.allowance(address(source), address(router)) == 0
                && link.allowance(address(source), address(router)) == 0
        );
        vm.chainId(84532);
        router.deliver(destination, message());
        require(
            destinationPool.balanceOf(address(destination)) == 8 ether
                && token.balanceOf(address(destination)) == 2 ether
        );
        Wallet.Deposit memory d = destination.getDeposit(bytes32(uint256(1)));
        require(d.state == Wallet.DepositState.DEPOSITED && d.amount == 8 ether);
    }

    function testDuplicateAndConcurrentNewOperationCannotSend() public {
        send();
        rejected(instruction());
        Wallet.Instruction memory i = instruction();
        i.operationId = keccak256("another");
        rejected(i);
        require(router.sends() == 1);
    }

    function testDepletedFeeBalanceBeforeWithdrawal() public {
        // Principal and all other prerequisites are funded; isolate only LINK depletion.
        vm.prank(address(source));
        link.transfer(address(this), 10 ether);
        rejected(instruction());
        require(pool.balanceOf(address(source)) == 8 ether && router.sends() == 0 && source.activeOperation() == 0);
    }

    function testFeeDriftAndUnsupportedLaneFailBeforeWithdrawal() public {
        router.setFee(2 ether);
        rejected(instruction());
        router.setFee(1 ether);
        router.setSupported(false);
        rejected(instruction());
        require(pool.balanceOf(address(source)) == 8 ether);
    }

    function testTransactionGasPriceCapStopsAssetMovement() public {
        bytes memory r = signed(instruction());
        vm.txGasPrice(2 gwei);
        vm.expectRevert();
        forwarder.deliver(source, metadata(), r);
        require(router.sends() == 0 && pool.balanceOf(address(source)) == 8 ether);
    }

    function testSourceRateExpirySignatureAndMetadataDrift() public {
        bytes memory report = signed(instruction());
        vm.prank(owner);
        pool.setCurrentLiquidityRate(5e25);
        vm.expectRevert();
        forwarder.deliver(source, metadata(), report);
        vm.prank(owner);
        pool.setCurrentLiquidityRate(4e25);
        vm.warp(1800);
        vm.expectRevert();
        forwarder.deliver(source, metadata(), report);
        vm.warp(1000);
        vm.expectRevert();
        forwarder.deliver(source, abi.encodePacked(WORKFLOW, bytes10("lending-p2"), owner), report);
        vm.expectRevert();
        source.onReport(metadata(), report);
        Wallet.Instruction memory i = instruction();
        i.amount = 7 ether;
        rejected(i);
    }

    function testOuterForwarderCanSucceedWithInnerFailure() public {
        router.setFee(2 ether);
        require(!forwarder.catchFailure(source, metadata(), signed(instruction())));
        require(router.sends() == 0);
    }

    function fillDestination() internal {
        token.mint(address(this), 995 ether);
        token.approve(address(destinationPool), 995 ether);
        destinationPool.supply(995 ether);
    }

    function testCaughtDepositFailureRetainsFullMessageAndRetriesWithoutSend() public {
        send();
        vm.chainId(84532);
        fillDestination();
        Client.Any2EVMMessage memory m = message();
        router.deliver(destination, m);
        Wallet.Deposit memory failed = destination.getDeposit(m.messageId);
        require(
            failed.state == Wallet.DepositState.IDLE && failed.reason.length > 0 && failed.amount == 8 ether
                && destination.reservedInbound() == 8 ether
        );
        require(
            keccak256(abi.encode(failed.message)) == keccak256(abi.encode(m))
                && destinationPool.balanceOf(address(destination)) == 0
        );
        vm.prank(owner);
        vm.expectRevert();
        destination.withdrawIdle(1);
        vm.prank(owner);
        vm.expectRevert();
        destination.depositToPool(1);
        vm.expectRevert();
        destination.retryDeposit(m.messageId);
        destinationPool.withdraw(995 ether, address(this));
        vm.prank(owner);
        destination.retryDeposit(m.messageId);
        require(
            destinationPool.balanceOf(address(destination)) == 8 ether && destination.reservedInbound() == 0
                && router.sends() == 1
        );
        vm.prank(owner);
        vm.expectRevert();
        destination.retryDeposit(m.messageId);
    }

    function testAllTokenEntriesAccountedAndFixedOwnerRecovery() public {
        send();
        vm.chainId(84532);
        fillDestination();
        Client.Any2EVMMessage memory m = message();
        m.destTokenAmounts = new Client.EVMTokenAmount[](2);
        m.destTokenAmounts[0] = Client.EVMTokenAmount(address(token), 3 ether);
        m.destTokenAmounts[1] = Client.EVMTokenAmount(address(token), 5 ether);
        router.deliver(destination, m);
        Wallet.Deposit memory failed = destination.getDeposit(m.messageId);
        require(failed.message.destTokenAmounts.length == 2);
        vm.expectRevert();
        destination.recoverFailedDeposit(m.messageId);
        vm.prank(owner);
        destination.recoverFailedDeposit(m.messageId);
        require(
            token.balanceOf(owner) == 8 ether && token.balanceOf(address(destination)) == 2 ether
                && destination.reservedInbound() == 0
        );
        vm.prank(owner);
        vm.expectRevert();
        destination.recoverFailedDeposit(m.messageId);
        vm.prank(owner);
        vm.expectRevert();
        destination.retryDeposit(keccak256("unknown"));
        require(router.sends() == 1);
    }

    function testReceiverRejectsWrongRouterPairTokensMalformedAndReplay() public {
        send();
        vm.chainId(84532);
        Client.Any2EVMMessage memory m = message();
        vm.expectRevert();
        destination.ccipReceive(m);
        m.sourceChainSelector = 10344971235874465080;
        vm.expectRevert();
        router.deliver(destination, m);
        m = message();
        m.sender = abi.encode(owner);
        vm.expectRevert();
        router.deliver(destination, m);
        m = message();
        m.data = hex"1234";
        vm.expectRevert();
        router.deliver(destination, m);
        m = message();
        m.destTokenAmounts[0].amount = 7 ether;
        vm.expectRevert();
        router.deliver(destination, m);
        m = message();
        link.mint(address(router), 8 ether);
        m.destTokenAmounts[0].token = address(link);
        vm.expectRevert();
        router.deliver(destination, m);
        m = message();
        router.deliver(destination, m);
        token.mint(address(router), 8 ether);
        vm.expectRevert();
        router.deliver(destination, m);
    }

    function testMainnetAndPeerRebindRejected() public {
        vm.prank(owner);
        vm.expectRevert();
        source.setPeer(owner);
        vm.chainId(1);
        vm.expectRevert();
        new Wallet(owner, pool, address(link), address(router), address(forwarder), WORKFLOW, POLICY, 1, 1);
    }

    function testOfficialMockForwarderRequiresEmptyMetadataAndIndependentOwnerSignature() public {
        address mock = 0x15fC6ae953E024d975e77382eEeC56A9101f9F88;
        vm.etch(mock, address(forwarder).code);
        source = new Wallet(owner, pool, address(link), address(router), mock, WORKFLOW, POLICY, 2 ether, 8 ether);
        vm.prank(owner);
        source.setPeer(address(destination));
        token.mint(address(source), 10 ether);
        link.mint(address(source), 10 ether);
        vm.prank(owner);
        source.depositToPool(8 ether);
        bytes memory r = signed(instruction());
        vm.expectRevert();
        P3Forwarder(mock).deliver(source, metadata(), r);
        Wallet.Instruction memory i = instruction();
        (uint8 v, bytes32 rr, bytes32 ss) = vm.sign(0xBAD, source.instructionDigest(i));
        vm.expectRevert();
        P3Forwarder(mock).deliver(source, hex"", abi.encode(i, abi.encodePacked(rr, ss, v)));
        P3Forwarder(mock).deliver(source, hex"", r);
        require(router.sends() == 1);
    }

    function testEip712DigestMatchesIndependentViemVector() public {
        address consumer = 0x2222222222222222222222222222222222222222;
        vm.etch(consumer, address(source).code);
        Wallet.Instruction memory i = Wallet.Instruction(
            hex"0101010101010101010101010101010101010101010101010101010101010101",
            11155111,
            consumer,
            10344971235874465080,
            0x3333333333333333333333333333333333333333,
            0x4444444444444444444444444444444444444444,
            0.8 ether,
            hex"5555555555555555555555555555555555555555555555555555555555555555",
            2000,
            1 ether,
            1 gwei,
            4e25,
            1000,
            hex"7777777777777777777777777777777777777777777777777777777777777777"
        );
        require(
            Wallet(consumer).instructionDigest(i)
                == hex"5fa87c18baef0383625f8ecce742f9bc98a3d748fe86a8d0b3f6b90e3695d859",
            "Owner signature ABI/domain differs from viem"
        );
    }
}

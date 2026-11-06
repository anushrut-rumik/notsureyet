// SPDX-License-Identifier: MIT
pragma solidity 0.8.26;
import {MockPool} from "../../src/lending/MockPool.sol";
import {TestStrategyWallet} from "../../src/lending/TestStrategyWallet.sol";
import {ERC20} from "@openzeppelin/contracts/token/ERC20/ERC20.sol";

interface LendingVm {
    function chainId(uint256) external;
    function etch(address, bytes calldata) external;
    function addr(uint256) external returns (address);
    function sign(uint256, bytes32) external returns (uint8, bytes32, bytes32);
    function prank(address) external;
    function expectRevert() external;
    function expectRevert(bytes4) external;
    function warp(uint256) external;
}

contract LendingToken is ERC20 {
    constructor() ERC20("Test", "TEST") {}

    function mint(address to, uint256 amount) external {
        _mint(to, amount);
    }
}

contract ReadOnlyRouter {
    function isChainSupported(uint64) external pure returns (bool) {
        return true;
    }
}

contract PublicMockForwarder {
    function deliver(TestStrategyWallet wallet, bytes calldata metadata, bytes calldata report) external {
        wallet.onReport(metadata, report);
    }
}

contract StrategyWalletTest {
    LendingVm constant vm = LendingVm(address(uint160(uint256(keccak256("hevm cheat code")))));
    uint256 constant SIGNER = 0x12345;
    bytes32 constant WORKFLOW = keccak256("test-workflow");
    bytes32 constant POLICY = keccak256("approved-policy-v1");
    address owner;
    LendingToken token;
    LendingToken linkToken;
    MockPool pool;
    TestStrategyWallet wallet;
    PublicMockForwarder forwarder;

    function setUp() public {
        vm.chainId(11155111);
        vm.warp(1000);
        owner = vm.addr(SIGNER);
        token = new LendingToken();
        linkToken = new LendingToken();
        pool = new MockPool(owner, address(token), 4e25, 1000 ether);
        forwarder = new PublicMockForwarder();
        wallet = new TestStrategyWallet(
            owner,
            pool,
            address(linkToken),
            address(new ReadOnlyRouter()),
            address(forwarder),
            WORKFLOW,
            POLICY,
            2 ether,
            8 ether
        );
        vm.prank(owner);
        wallet.setPeer(address(0xBEEF));
        token.mint(address(wallet), 10 ether);
        vm.prank(owner);
        wallet.depositToPool(8 ether);
    }

    function instruction() internal view returns (TestStrategyWallet.Instruction memory) {
        return TestStrategyWallet.Instruction(
            keccak256("operation-1"),
            11155111,
            address(wallet),
            10344971235874465080,
            address(0xBEEF),
            address(token),
            8 ether,
            POLICY,
            2000
        );
    }

    function metadata() internal view returns (bytes memory) {
        return abi.encodePacked(WORKFLOW, bytes10("lending-p2"), owner, bytes2(0));
    }

    function report(TestStrategyWallet.Instruction memory i, uint256 key) internal returns (bytes memory) {
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(key, wallet.instructionDigest(i));
        return abi.encode(i, abi.encodePacked(r, s, v));
    }

    function testInitialDepositAndReserveAndNoAccrual() public {
        require(pool.balanceOf(address(wallet)) == 8 ether && token.balanceOf(address(wallet)) == 2 ether);
        require(token.allowance(address(wallet), address(pool)) == 0);
        vm.warp(1000000);
        require(pool.balanceOf(address(wallet)) == 8 ether);
    }

    function testAllMoneyMovingMethodsRejectStranger() public {
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        wallet.depositToPool(1);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        wallet.withdrawFromPool(1);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        wallet.withdrawIdle(1);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        wallet.recoverLink(1);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        wallet.setPeer(address(this));
        vm.expectRevert(MockPool.Unauthorized.selector);
        pool.setCurrentLiquidityRate(1e25);
        vm.expectRevert();
        pool.withdraw(1, address(this));
    }

    function testOwnerCannotDepositOrWithdrawProtectedReserve() public {
        vm.prank(owner);
        vm.expectRevert();
        wallet.depositToPool(1);
        vm.prank(owner);
        vm.expectRevert();
        wallet.withdrawIdle(1);
    }

    function testPublicCrossChainEntryAlwaysDisabled() public {
        vm.expectRevert(TestStrategyWallet.Part3Disabled.selector);
        wallet.withdrawFromPoolAndDepositCrossChain(instruction());
    }

    function testForgedSimulatorMetadataWithoutOwnerSignatureFails() public {
        bytes memory bad = report(instruction(), 0xBAD);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        forwarder.deliver(wallet, metadata(), bad);
        require(pool.balanceOf(address(wallet)) == 8 ether);
    }

    function testValidSignedReportConsumesOperationWithoutMovingMoney() public {
        TestStrategyWallet.Instruction memory i = instruction();
        bytes memory encoded = report(i, SIGNER);
        forwarder.deliver(wallet, metadata(), encoded);
        require(wallet.consumed(i.operationId));
        require(pool.balanceOf(address(wallet)) == 8 ether && token.balanceOf(address(wallet)) == 2 ether);
        vm.expectRevert(TestStrategyWallet.InvalidInstruction.selector);
        forwarder.deliver(wallet, metadata(), encoded);
    }

    function testWrongForwarderAndWorkflowIdentityRejected() public {
        bytes memory encoded = report(instruction(), SIGNER);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        wallet.onReport(metadata(), encoded);
        bytes memory badMeta = abi.encodePacked(bytes32(0), bytes10("lending-p2"), owner);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        forwarder.deliver(wallet, badMeta, encoded);
        badMeta = abi.encodePacked(WORKFLOW, bytes10("wrong"), owner);
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        forwarder.deliver(wallet, badMeta, encoded);
        badMeta = abi.encodePacked(WORKFLOW, bytes10("lending-p2"), address(this));
        vm.expectRevert(TestStrategyWallet.Unauthorized.selector);
        forwarder.deliver(wallet, badMeta, encoded);
    }

    function testEachInstructionFieldIsBoundAndValidated() public {
        for (uint256 n; n < 9; n++) {
            TestStrategyWallet.Instruction memory i = instruction();
            bytes memory original = report(i, SIGNER);
            (, bytes memory signature) = abi.decode(original, (TestStrategyWallet.Instruction, bytes));
            if (n == 0) i.operationId = keccak256("changed");
            if (n == 1) i.sourceChainId = 84532;
            if (n == 2) i.consumer = address(this);
            if (n == 3) i.destinationSelector = 16015286601757825753;
            if (n == 4) i.destination = address(this);
            if (n == 5) i.token = address(this);
            if (n == 6) i.amount = 7 ether;
            if (n == 7) i.policyVersion = keccak256("v2");
            if (n == 8) i.expiry = 3000;
            vm.expectRevert();
            forwarder.deliver(wallet, metadata(), abi.encode(i, signature));
        }
    }

    function testExpiryPolicyRevocationAndOverLimit() public {
        TestStrategyWallet.Instruction memory i = instruction();
        bytes memory encoded = report(i, SIGNER);
        vm.warp(2000);
        vm.expectRevert();
        forwarder.deliver(wallet, metadata(), encoded);
        vm.warp(1000);
        vm.prank(owner);
        wallet.setPolicyVersion(keccak256("revoked"));
        vm.expectRevert();
        forwarder.deliver(wallet, metadata(), encoded);
        vm.prank(owner);
        wallet.setPolicyVersion(POLICY);
        i.amount = 9 ether;
        encoded = report(i, SIGNER);
        vm.expectRevert();
        forwarder.deliver(wallet, metadata(), encoded);
    }

    function testCalldataIsNotRawAbiTuple() public {
        TestStrategyWallet.Instruction memory i = instruction();
        bytes memory raw = report(i, SIGNER);
        (, bytes memory sig) = abi.decode(raw, (TestStrategyWallet.Instruction, bytes));
        bytes memory functionCalldata = abi.encodeWithSignature(
            "admit((bytes32,uint256,address,uint64,address,address,uint256,bytes32,uint256),bytes)", i, sig
        );
        require(keccak256(raw) != keccak256(functionCalldata));
        vm.expectRevert();
        forwarder.deliver(wallet, metadata(), functionCalldata);
        vm.expectRevert();
        forwarder.deliver(wallet, metadata(), bytes.concat(raw, hex"00"));
        forwarder.deliver(wallet, metadata(), raw);
    }

    function testTwoUsersCannotWithdrawEachOthersPosition() public {
        MockPool otherPool = new MockPool(address(this), address(token), 4e25, 1000 ether);
        TestStrategyWallet other = new TestStrategyWallet(
            address(this),
            otherPool,
            address(linkToken),
            address(new ReadOnlyRouter()),
            address(forwarder),
            WORKFLOW,
            POLICY,
            1 ether,
            9 ether
        );
        token.mint(address(other), 10 ether);
        other.depositToPool(9 ether);
        other.withdrawFromPool(9 ether);
        other.withdrawIdle(9 ether);
        require(pool.balanceOf(address(wallet)) == 8 ether && token.balanceOf(address(wallet)) == 2 ether);
    }

    function testEip712DigestMatchesIndependentViemVector() public {
        address consumer = 0x2222222222222222222222222222222222222222;
        vm.etch(consumer, address(wallet).code);
        TestStrategyWallet.Instruction memory i = TestStrategyWallet.Instruction(
            hex"0101010101010101010101010101010101010101010101010101010101010101",
            11155111,
            consumer,
            10344971235874465080,
            0x3333333333333333333333333333333333333333,
            0x4444444444444444444444444444444444444444,
            123,
            hex"5555555555555555555555555555555555555555555555555555555555555555",
            2000
        );
        require(
            TestStrategyWallet(consumer).instructionDigest(i)
                == hex"8581f7f2db65b58aad79da6b4d5bc42e866d2a65d99fb32fb7d2975177365d9d",
            "Wrong standard typed-data digest"
        );
    }

    function testInboundCcipAndUnknownSenderAlwaysRejected() public {
        (bool success,) = address(wallet).call(abi.encodeWithSignature("ccipReceive(bytes)", hex"1234"));
        require(!success, "Part 2 must reject every source/sender pair");
    }

    function testRejectsMainnetConstruction() public {
        address router = address(new ReadOnlyRouter());
        vm.chainId(1);
        vm.expectRevert();
        new TestStrategyWallet(owner, pool, address(linkToken), router, address(forwarder), WORKFLOW, POLICY, 1, 1);
    }
}

import warnings

import capstone as cs
import collections

from .abstractcpu import Cpu, RegisterFile, Abi, SyscallAbi, Operand, instruction
from .arm import HighBit, Armv7Operand
from .register import Register
from ...core.smtlib import Operators


# Map different instructions to a single implementation.
# XXX: Avoiding this for now.
INSTRUCTION_MAPPINGS = {
    # 'MOVZ': 'MOV'
}


class Aarch64RegisterFile(RegisterFile):
    Regspec = collections.namedtuple('RegSpec', 'parent size')

    # Register table.
    _table = {}

    # From "B1.2 Registers in AArch64 Execution state" in the ARM Architecture
    # Reference Manual: ARMv8, for ARMv8-A architecture profile:
    #
    # 31 general-purpose registers, R0 to R30.
    # Each register can be accessed as:
    # * A 64-bit general-purpose register named X0 to X30.
    # * A 32-bit general-purpose register named W0 to W30.
    for i in range(31):
        _table[f'X{i}'] = Regspec(f'X{i}', 64)
        _table[f'W{i}'] = Regspec(f'X{i}', 32)

    # A 64-bit dedicated Stack Pointer register.  The least significant 32 bits
    # of the stack-pointer can be accessed via the register name WSP.
    #
    # The use of SP as an operand in an instruction, indicates the use of the
    # current stack pointer.
    _table['SP']  = Regspec('SP', 64)
    _table['WSP'] = Regspec('SP', 32)

    # A 64-bit Program Counter holding the address of the current instruction.
    # Software cannot write directly to the PC.  It can only be updated on a
    # branch, exception entry or exception return.
    #
    # Attempting to execute an A64 instruction that is not word-aligned
    # generates a PC alignment fault, see "PC alignment checking".
    _table['PC'] = Regspec('PC', 64)

    # 32 SIMD&FP registers, V0 to V31.
    # Each register can be accessed as:
    # * A 128-bit register named Q0 to Q31.
    # * A 64-bit register named D0 to D31.
    # * A 32-bit register named S0 to S31.
    # * A 16-bit register named H0 to H31.
    # * An 8-bit register named B0 to B31.
    # * A 128-bit vector of elements.
    # * A 64-bit vector of elements.
    #
    # Where the number of bits described by a register name does not occupy an entire
    # SIMD&FP register, it refers to the least significant bits.
    for i in range(32):
        _table[f'Q{i}'] = Regspec(f'Q{i}', 128)
        _table[f'D{i}'] = Regspec(f'Q{i}', 64)
        _table[f'S{i}'] = Regspec(f'Q{i}', 32)
        _table[f'H{i}'] = Regspec(f'Q{i}', 16)
        _table[f'B{i}'] = Regspec(f'Q{i}', 8)
        # XXX: Support vectors.
        # For more information about data types and vector formats, see "Supported
        # data types".

    # Two SIMD and floating-point control and status registers, FPCR and FPSR.
    _table['FPCR'] = Regspec('FPCR', 64)
    _table['FPSR'] = Regspec('FPSR', 64)

    # Condition flags.
    # Flag-setting instructions set these.
    #
    # N Negative Condition flag.  If the result of the instruction is regarded
    # as a two's complement signed integer, the PE sets this to:
    # * 1 if the result is negative.
    # * 0 if the result is positive or zero.
    #
    # Z Zero Condition flag.  Set to:
    # * 1 if the result of the instruction is zero.
    # * 0 otherwise.
    # A result of zero often indicates an equal result from a comparison.
    #
    # C Carry Condition flag.  Set to:
    # * 1 if the instruction results in a carry condition, for example an unsigned overflow
    #   that is the result of an addition.
    # * 0 otherwise.
    #
    # V Overflow Condition flag.  Set to:
    # * 1 if the instruction results in an overflow condition, for example a signed
    #   overflow that is the result of an addition.
    # * 0 otherwise.
    #
    # Conditional instructions test the N, Z, C and V Condition flags, combining them
    # with the Condition code for the instruction to determine whether the instruction
    # must be executed.  In this way, execution of the instruction is conditional on
    # the result of a previous operation.  For more information about conditional
    # execution, see "Condition flags and related instructions".
    #
    # Also see "C5.2.9 NZCV, Condition Flags".
    # Counting from the right:
    # N, bit [31]
    # Z, bit [30]
    # C, bit [29]
    # V, bit [28]
    _table['NZCV'] = Regspec('NZCV', 64)

    # From "C1.2.5 Register names":
    # Zero register.
    _table['XZR'] = Regspec('XZR', 64)
    _table['WZR'] = Regspec('XZR', 32)

    def __init__(self):
        # Register aliases.
        _aliases = {
            # This one is required by the 'Cpu' class.
            'STACK': 'SP',
            # From "5.1 Machine Registers" in Procedure Call Standard for the ARM 64-bit
            # Architecture (AArch64):
            # Frame pointer.
            'FP': 'X29',
            # Intra-procedure-call temporary registers (can be used by call veneers
            # and PLT code); at other times may be used as temporary registers.
            'IP1': 'X17',
            'IP0': 'X16',
            # From "B1.2 Registers in AArch64 Execution state" in the ARM Architecture
            # Reference Manual: ARMv8, for ARMv8-A architecture profile:
            # The X30 general-purpose register is used as the procedure call
            # link register.
            'LR': 'X30'
        }
        super().__init__(_aliases)

        # Used for validity checking.
        self._all_registers = set()

        # Initialize the register cache.
        # Only the full registers are stored here (called "parents").
        # If a smaller register is used, it must find its "parent" in order to
        # be stored here.
        self._registers = {}
        for name in self._table.keys():
            self._all_registers.add(name)

            parent, size = self._table[name]
            if name != parent:
                continue
            self._registers[name] = Register(size)


    def read(self, register):
        assert register in self
        name = self._alias(register)
        parent, size = self._table[name]
        value = self._registers[parent].read()

        if name != parent:
            _, parent_size = self._table[parent]
            if size < parent_size:
                value = Operators.EXTRACT(value, 0, size)

        return value

    def write(self, register, value):
        assert register in self
        name = self._alias(register)
        parent, size = self._table[name]
        assert value <= 2 ** size - 1
        self._registers[parent].write(value)

    @property
    def canonical_registers(self):
        # XXX: This one is used by 'UnicornEmulator._step'.
        # And Unicorn doesn't support these.
        not_supported = set()
        not_supported.add('FPSR')
        not_supported.add('FPCR')
        return self._all_registers - not_supported

    @property
    def all_registers(self):
        return self._all_registers


# XXX: Update/rewrite this.
class Aarch64Cpu(Cpu):
    """
    Cpu specialization handling the ARM64 architecture.
    """
    address_bit_size = 64
    max_instr_width = 4
    # XXX: Possible values: 'aarch64_be', 'aarch64', 'armv8b', 'armv8l'.
    # See 'UTS_MACHINE' and 'COMPAT_UTS_MACHINE' in the Linux kernel source.
    # https://stackoverflow.com/a/45125525
    machine = 'aarch64'
    arch = cs.CS_ARCH_ARM64
    # Though there is no thumb mode in aarch64, we still have to set it as some other components may require it
    # see https://stackoverflow.com/questions/46086329/can-i-use-thumb-instructions-in-an-arm64-binary
    mode = cs.CS_ARCH_ARM

    def __init__(self, memory):
        warnings.warn('Aarch64 support is experimental')
        self._last_flags = {'C': 0, 'V': 0, 'N': 0, 'Z': 0, 'GE': 0}
        super(Aarch64Cpu, self).__init__(Aarch64RegisterFile(), memory)

    def __getstate__(self):
        state = super(Aarch64Cpu, self).__getstate__()
        state['_last_flags'] = self._last_flags
        # TODO / FIXME / REVIEWME: do we need those in aarch64? [copied from armv7]
        # state['at_symbolic_conditional'] = self._at_symbolic_conditional
        # state['_it_conditional'] = self._it_conditional
        return state

    def __setstate__(self, state):
        self._last_flags = state['_last_flags']
        # TODO / FIXME / REVIEWME: do we need those in aarch64? [copied from armv7]
        # self._at_symbolic_conditional = state['at_symbolic_conditional']
        # self._it_conditional = state['_it_conditional']
        super(Aarch64Cpu, self).__setstate__(state)

    def _wrap_operands(self, ops):
        return [Aarch64Operand(self, op) for op in ops]

    @instruction
    def MOV(cpu, dest, src):
        """
        Implement the MOV{S} instruction.

        Note: If src operand is PC, temporarily release our logical PC
        view and conform to the spec, which dictates PC = curr instr + 8

        :param Arm64Operand dest: The destination operand; register.
        :param Arm64Operand src: The source operand; register or immediate.
        """
        # XXX: Finish this.
        result = src.read()
        dest.write(result)
        # cpu.set_flags(C=carry_out, N=HighBit(result), Z=(result == 0))

    @staticmethod
    def canonicalize_instruction_name(instr):
        name = instr.insn_name().upper()
        # XXX bypass a capstone bug that incorrectly labels some insns as mov
        if name == 'MOV':
            if instr.mnemonic.startswith('lsr'):
                return 'LSR'
            elif instr.mnemonic.startswith('lsl'):
                return 'LSL'
            elif instr.mnemonic.startswith('asr'):
                return 'ASR'
        return INSTRUCTION_MAPPINGS.get(name, name)

    # Flags that are the result of arithmetic instructions. Unconditionally
    # set, but conditionally committed.
    #
    # Register file has the actual CPU flags
    def set_flags(self, **flags):
        # """
        # Note: For any unmodified flags, update _last_flags with the most recent
        # committed value. Otherwise, for example, this could happen:

        #     overflow=0
        #     instr1 computes overflow=1, updates _last_flags, doesn't commit
        #     instr2 updates all flags in _last_flags except overflow (overflow remains 1 in _last_flags)
        #     instr2 commits all in _last_flags
        #     now overflow=1 even though it should still be 0
        # """
        # unupdated_flags = self._last_flags.viewkeys() - flags.viewkeys()
        # for flag in unupdated_flags:
        #     flag_name = 'APSR_{}'.format(flag)
        #     self._last_flags[flag] = self.regfile.read(flag_name)
        # self._last_flags.update(flags)
        pass


class Aarch64CdeclAbi(Abi):
    """Aarch64/arm64 cdecl function call ABI"""

    # XXX: Floating-point arguments are not supported.
    # For floats and doubles, the first 8 arguments are passed via registers
    # (S0-S7 for floats, D0-D7 for doubles), then on stack.
    # The result is returned in S0 for floats and D0 for doubles.

    def get_arguments(self):
        # First 8 arguments are passed via X0-X7 (or W0-W7 if they are 32-bit),
        # then on stack.

        for reg in ('X0', 'X1', 'X2', 'X3', 'X4', 'X5', 'X6', 'X7'):
            yield reg

        for address in self.values_from(self._cpu.STACK):
            yield address

    def write_result(self, result):
        self._cpu.X0 = result

    def ret(self):
        self._cpu.PC = self._cpu.LR


class Aarch64LinuxSyscallAbi(SyscallAbi):
    """Aarch64/arm64 Linux system call ABI"""

    # From 'man 2 syscall':
    # arch/ABI:       arm64
    # instruction:    svc #0
    # syscall number: x8
    # arguments:      x0-x5 (arg1-arg6)
    # return value:   x0
    def syscall_number(self):
        return self._cpu.X8

    def get_arguments(self):
        return ('X{}'.format(i) for i in range(6))

    def write_result(self, result):
        self._cpu.X0 = result


# XXX: Update/rewrite this.
class Aarch64Operand(Operand):
    def __init__(self, cpu, op, **kwargs):
        super(Aarch64Operand, self).__init__(cpu, op)

        assert self.op.type in (
            cs.arm64.ARM64_OP_REG,  # Register operand
            cs.arm64.ARM64_OP_MEM,  # Memory operand
            cs.arm64.ARM64_OP_IMM,  # Immediate operand
            cs.arm64.ARM64_OP_CIMM,  # C-Immediate
            cs.arm64.ARM64_OP_FP  # Floating-Point operand
        )

        self._type = self.op.type

    @property
    def type(self):
        return self._type

    def read(self, nbits=None, with_carry=False):
        # XXX: Finish this.
        assert nbits is None
        assert not with_carry

        if self.type == cs.arm64.ARM64_OP_REG:
            return self.cpu.regfile.read(self.reg)
        elif self.type == cs.arm64.ARM64_OP_IMM:
            return self.op.imm
        else:
            raise NotImplementedError(f"Unsupported operand type: '{self.type}'")

        # carry = self.cpu.regfile.read('APSR_C')
        # if self.type == cs.arm64.ARM64_OP_REG:
        #     value = self.cpu.regfile.read(self.reg)
        #     # PC in this case has to be set to the instruction after next. PC at this point
        #     # is already pointing to next instruction; we bump it one more.
        #     if self.reg in ('PC', 'R15'):
        #         value += self.cpu.instruction.size
        #     if self.is_shifted():
        #         shift = self.op.shift
        #         value, carry = self.cpu._shift(value, shift.type, shift.value, carry)
        #     if with_carry:
        #         return value, carry
        #     return value
        # elif self.type == cs.arm64.ARM64_OP_IMM:
        #     imm = self.op.imm
        #     if with_carry:
        #         return imm, self._get_expand_imm_carry(carry)
        #     return imm
        # elif self.type == 'coprocessor':
        #     imm = self.op.imm
        #     return imm
        # elif self.type == 'memory':
        #     val = self.cpu.read_int(self.address(), nbits)
        #     if with_carry:
        #         return val, carry
        #     return val
        # else:
        #     raise NotImplementedError("readOperand unknown type", self.op.type)

    def write(self, value, nbits=None):
        # XXX: Finish this.
        assert nbits is None

        if self.type == cs.arm64.ARM64_OP_REG:
            self.cpu.regfile.write(self.reg, value)
        else:
            raise NotImplementedError(f"Unsupported operand type: '{self.type}'")

        # if self.type == cs.arm64.ARM64_OP_REG:
        #     self.cpu.regfile.write(self.reg, value)
        # elif self.type == 'memory':
        #     raise NotImplementedError('need to impl arm store mem')
        # else:
        #     raise NotImplementedError("writeOperand unknown type", self.op.type)

    # def writeback(self, value):
    #     if self.type == cs.arm64.ARM64_OP_REG:
    #         self.write(value)
    #     elif self.type == 'memory':
    #         self.cpu.regfile.write(self.mem.base, value)
    #     else:
    #         raise NotImplementedError("writeback Operand unknown type", self.op.type)

    # def is_shifted(self):
    #     return self.op.shift.type != cs.arm.ARM_SFT_INVALID

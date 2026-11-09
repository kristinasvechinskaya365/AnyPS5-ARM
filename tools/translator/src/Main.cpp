#include "Elf.hpp"
#include "Lifter.hpp"

#include <capstone/capstone.h>
#include <llvm/Config/llvm-config.h>
#include <llvm/IR/LLVMContext.h>
#include <llvm/IR/LegacyPassManager.h>
#include <llvm/IR/Module.h>
#include <llvm/IR/Verifier.h>
#include <llvm/MC/TargetRegistry.h>
#include <llvm/MC/MCSubtargetInfo.h>
#include <llvm/Passes/PassBuilder.h>
#include <llvm/Support/FileSystem.h>
#include <llvm/Support/JSON.h>
#include <llvm/Support/TargetSelect.h>
#include <llvm/Support/raw_ostream.h>
#include <llvm/Target/TargetMachine.h>
#include <llvm/TargetParser/Triple.h>

#include <cstdio>
#include <exception>
#include <memory>
#include <string>
#include <vector>

namespace {

void Optimize(llvm::Module& module, llvm::TargetMachine& machine) {
    llvm::LoopAnalysisManager loops;
    llvm::FunctionAnalysisManager functions;
    llvm::CGSCCAnalysisManager cgscc;
    llvm::ModuleAnalysisManager modules;
    llvm::PassBuilder builder(&machine);
    builder.registerModuleAnalyses(modules);
    builder.registerCGSCCAnalyses(cgscc);
    builder.registerFunctionAnalyses(functions);
    builder.registerLoopAnalyses(loops);
    builder.crossRegisterProxies(loops, functions, cgscc, modules);
    builder.buildPerModuleDefaultPipeline(llvm::OptimizationLevel::O2).run(module, modules);
}

}

// aps5-translator <guest.elf> <output.o> [--emit-llvm]: lifts the guest's x86-64 code and writes an
// arm64 Mach-O object to link with the translator runtime and the host libraries.
int main(int argc, char** argv) {
    const auto usage = [&] {
        std::fprintf(stderr, "usage: %s <guest.elf> <output.o> [--emit-llvm] [--cpu <name>] | --build-info [--cpu <name>]\n", argv[0]);
    };
    std::string cpu = "apple-m1";
    std::vector<std::string> paths;
    bool emitLlvm = false;
    bool buildInfo = false;
    bool selectedCpu = false;
    for (int index = 1; index < argc; ++index) {
        const std::string argument(argv[index]);
        if (argument == "--emit-llvm" && !emitLlvm) emitLlvm = true;
        else if (argument == "--build-info" && !buildInfo) buildInfo = true;
        else if (argument == "--cpu" && !selectedCpu && index + 1 < argc) {
            cpu = argv[++index];
            selectedCpu = true;
        } else if (argument.starts_with("--")) {
            usage();
            std::fprintf(stderr, "unknown, repeated or incomplete option: %s\n", argument.c_str());
            return 2;
        } else paths.push_back(argument);
    }
    if ((buildInfo && (!paths.empty() || emitLlvm)) || (!buildInfo && paths.size() != 2)) {
        usage();
        return 2;
    }
    try {
        LLVMInitializeAArch64TargetInfo();
        LLVMInitializeAArch64Target();
        LLVMInitializeAArch64TargetMC();
        LLVMInitializeAArch64AsmPrinter();
        const llvm::Triple triple("arm64-apple-macosx13.0.0");
        std::string error;
        const auto* target = llvm::TargetRegistry::lookupTarget(triple, error);
        if (target == nullptr) {
            std::fprintf(stderr, "%s\n", error.c_str());
            return 1;
        }
#if LLVM_VERSION_MAJOR >= 22
        const auto subtargetTriple = triple;
#else
        const auto subtargetTriple = triple.str();
#endif
        std::unique_ptr<llvm::MCSubtargetInfo> subtarget(target->createMCSubtargetInfo(subtargetTriple, "apple-m1", ""));
        if (subtarget == nullptr || !subtarget->isCPUStringValid(cpu)) {
            std::fprintf(stderr, "CPU '%s' is not supported by LLVM %s for %s\n", cpu.c_str(), LLVM_VERSION_STRING, triple.str().c_str());
            return 2;
        }
        if (buildInfo) {
            int capstoneMajor = 0, capstoneMinor = 0;
            cs_version(&capstoneMajor, &capstoneMinor);
            llvm::outs() << llvm::json::Value(llvm::json::Object{
                {"schema_version", 1}, {"llvm_version", LLVM_VERSION_STRING}, {"target_triple", triple.str()},
                {"target_cpu", cpu}, {"optimization", "O2"},
                {"capstone_api_version", std::to_string(capstoneMajor) + "." + std::to_string(capstoneMinor)}}) << '\n';
            return 0;
        }
        std::unique_ptr<llvm::TargetMachine> machine(target->createTargetMachine(triple, cpu, "", {}, llvm::Reloc::PIC_));
        if (machine == nullptr) {
            std::fprintf(stderr, "cannot create target machine for CPU '%s'\n", cpu.c_str());
            return 1;
        }
        const Translator::Elf elf(paths[0]);
        llvm::LLVMContext context;
        Translator::Lifter lifter(elf, context);
        lifter.Discover();
        auto module = lifter.Lift();
        if (llvm::verifyModule(*module, &llvm::errs())) {
            if (emitLlvm) module->print(llvm::errs(), nullptr);
            std::fprintf(stderr, "lifted module does not verify\n");
            return 1;
        }
        module->setTargetTriple(triple);
        module->setDataLayout(machine->createDataLayout());
        Optimize(*module, *machine);

        std::error_code code;
        llvm::raw_fd_ostream output(paths[1], code, llvm::sys::fs::OF_None);
        if (code) {
            std::fprintf(stderr, "cannot write %s: %s\n", paths[1].c_str(), code.message().c_str());
            return 1;
        }
        if (emitLlvm) {
            module->print(output, nullptr);
        } else {
            llvm::legacy::PassManager passes;
            if (machine->addPassesToEmitFile(passes, output, nullptr, llvm::CodeGenFileType::ObjectFile)) {
                std::fprintf(stderr, "the target cannot emit an object file\n");
                return 1;
            }
            passes.run(*module);
        }

        const auto& stats = lifter.Stats();
        std::fprintf(stderr, "%zu functions, %zu instructions lifted\n", stats.functions, stats.instructions);
        for (const auto& [what, count] : stats.unsupported) std::fprintf(stderr, "  unsupported: %s (%zu)\n", what.c_str(), count);
    } catch (const std::exception& exception) {
        std::fprintf(stderr, "%s\n", exception.what());
        return 1;
    }
    return 0;
}

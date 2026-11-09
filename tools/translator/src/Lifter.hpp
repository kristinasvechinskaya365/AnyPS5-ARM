#ifndef TOOLS_TRANSLATOR_SRC_LIFTER_HPP
#define TOOLS_TRANSLATOR_SRC_LIFTER_HPP

#include "Elf.hpp"

#include <cstdint>
#include <map>
#include <memory>
#include <set>
#include <string>
#include <vector>

namespace llvm {
class LLVMContext;
class Module;
}

namespace Translator {

struct Statistics {
    std::size_t functions = 0;
    std::size_t instructions = 0;
    std::map<std::string, std::size_t> unsupported;
};

class Lifter {
public:
    Lifter(const Elf& elf, llvm::LLVMContext& context);
    ~Lifter();

    void Discover();
    std::unique_ptr<llvm::Module> Lift();
    const Statistics& Stats() const { return stats; }

private:
    struct Impl;
    std::unique_ptr<Impl> impl;
    Statistics stats;
};

}

#endif

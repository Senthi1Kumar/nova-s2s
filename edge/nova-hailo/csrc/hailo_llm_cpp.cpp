// Opt-in native LLM backend: wraps hailort::genai::LLM directly (not
// hailo_platform.genai's Python binding) so the blocking NPU read loop can
// explicitly release the GIL, letting the Python-side TTS worker thread
// actually get scheduled during decode. See ROADMAP.md #6d / Sprint 1b.
//
// Compile against the *on-device* HailoRT (scripts/build_hailo_llm_cpp.sh).
// 5.1.1: generate(params, prompts)
// 5.2.0+: generate(params, prompts, tools_json_strings={})  — native tools
// 5.3.0: same generate; LLMGeneratorCompletion::read_all default timeout is
//        10 minutes. We stream via read() with a matching per-call timeout.
//
// JSON message serialization stays in Python (json.dumps per message) --
// this file only ever sees pre-built JSON strings.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <chrono>
#include <memory>
#include <mutex>
#include <stdexcept>
#include <string>
#include <vector>

#include "hailo/genai/llm/llm.hpp"
#include "hailo/hailort.h"
#include "hailo/vdevice.hpp"

namespace py = pybind11;

#ifndef HAILO_GENAI_LLM_HAS_TOOLS
#if defined(HAILORT_MAJOR_VERSION) && defined(HAILORT_MINOR_VERSION)
#if (HAILORT_MAJOR_VERSION > 5) || \
    (HAILORT_MAJOR_VERSION == 5 && HAILORT_MINOR_VERSION >= 2)
#define HAILO_GENAI_LLM_HAS_TOOLS 1
#endif
#endif
#endif

#ifndef HAILO_GENAI_LLM_HAS_LONG_READ_TIMEOUT
#if defined(HAILORT_MAJOR_VERSION) && defined(HAILORT_MINOR_VERSION)
#if (HAILORT_MAJOR_VERSION > 5) || \
    (HAILORT_MAJOR_VERSION == 5 && HAILORT_MINOR_VERSION >= 3)
#define HAILO_GENAI_LLM_HAS_LONG_READ_TIMEOUT 1
#endif
#endif
#endif

#define NOVA_STR_HELPER(x) #x
#define NOVA_STR(x) NOVA_STR_HELPER(x)

#if defined(HAILORT_MAJOR_VERSION) && defined(HAILORT_MINOR_VERSION) && \
    defined(HAILORT_REVISION_VERSION)
static const char *kCompiledHailoRT = NOVA_STR(HAILORT_MAJOR_VERSION) "." NOVA_STR(
    HAILORT_MINOR_VERSION) "." NOVA_STR(HAILORT_REVISION_VERSION);
#elif defined(HAILORT_MAJOR_VERSION) && defined(HAILORT_MINOR_VERSION)
static const char *kCompiledHailoRT =
    NOVA_STR(HAILORT_MAJOR_VERSION) "." NOVA_STR(HAILORT_MINOR_VERSION);
#else
static const char *kCompiledHailoRT = "unknown";
#endif

#ifdef HAILO_GENAI_LLM_HAS_LONG_READ_TIMEOUT
// HailoRT 5.3 changelog: read_all default timeout is 10 minutes. Prefill on
// Hailo-10H can exceed the older short read() default on a cold KV cache.
static constexpr auto kTokenReadTimeout = std::chrono::milliseconds(10 * 60 * 1000);
#endif

static std::string status_msg(const char *what, hailo_status status) {
    return std::string(what) + ", status=" + std::to_string(static_cast<int>(status));
}

class HailoLLMCpp {
public:
    // group_id must match pipeline.py's SHARED_VDEVICE_GROUP_ID (params.group_id
    // there) -- on a single Hailo-10H, a VDevice created without joining that
    // same group requests an exclusive physical device and fails with
    // HAILO_OUT_OF_PHYSICAL_DEVICES once the Python-side VDevice already holds
    // the one available device.
    HailoLLMCpp(const std::string &hef_path, float temperature, uint32_t seed,
                uint32_t max_tokens, const std::string &group_id)
        : group_id_(group_id),
          temperature_(temperature),
          seed_(seed),
          default_max_tokens_(max_tokens) {
        hailo_vdevice_params_t params;
        hailo_status init_status = hailo_init_vdevice_params(&params);
        if (HAILO_SUCCESS != init_status) {
            throw std::runtime_error(status_msg("hailo_init_vdevice_params failed",
                                                init_status));
        }
        params.group_id = group_id_.c_str();

        auto vdevice_exp = hailort::VDevice::create_shared(params);
        if (!vdevice_exp) {
            throw std::runtime_error(
                status_msg("VDevice::create_shared failed", vdevice_exp.status()));
        }
        vdevice_ = vdevice_exp.release();

        hailort::genai::LLMParams llm_params(hef_path, "", true);
        auto llm_exp = hailort::genai::LLM::create(vdevice_, llm_params);
        if (!llm_exp) {
            throw std::runtime_error(
                status_msg("LLM::create failed", llm_exp.status()));
        }
        llm_ = std::make_shared<hailort::genai::LLM>(llm_exp.release());
    }

    // Runs the full generate+read loop with the GIL released; re-acquires it
    // only for the duration of each token_callback invocation. Returns the
    // full generated text (already token_callback-visited piece by piece).
    //
    // tools_json_strings: HailoRT 5.2+ native tool schemas (OpenAI-style JSON
    // strings). Empty keeps host-side routing (enable_in_prompt: false). Tools
    // + system messages are only legal on a fresh context.
    std::string generate(const std::vector<std::string> &prompt_json_strings,
                          uint32_t max_tokens, py::object token_callback,
                          py::object should_stop,
                          const std::vector<std::string> &tools_json_strings) {
        std::string full_text;
        bool stopped_early = false;

        {
            // Drop the GIL *before* taking generate_mu_ so another Python
            // thread blocked on generate() is not holding the GIL.
            py::gil_scoped_release release_gil;
            std::lock_guard<std::mutex> generate_lock(generate_mu_);

            auto params_exp = llm_->create_generator_params();
            if (!params_exp) {
                throw std::runtime_error(
                    status_msg("create_generator_params failed", params_exp.status()));
            }
            auto params = params_exp.release();
            params.set_temperature(temperature_);
            params.set_seed(seed_);
            params.set_max_generated_tokens(
                max_tokens > 0 ? max_tokens : default_max_tokens_);

#ifdef HAILO_GENAI_LLM_HAS_TOOLS
            auto completion_exp =
                llm_->generate(params, prompt_json_strings, tools_json_strings);
#else
            if (!tools_json_strings.empty()) {
                throw std::runtime_error(
                    "native LLM tools require HailoRT >= 5.2 (this .so was "
                    "compiled against " +
                    std::string(kCompiledHailoRT) + ")");
            }
            auto completion_exp = llm_->generate(params, prompt_json_strings);
#endif
            if (!completion_exp) {
                throw std::runtime_error(
                    status_msg("LLM::generate failed", completion_exp.status()));
            }
            auto completion = completion_exp.release();
            struct AbortIfStillGenerating {
                hailort::genai::LLMGeneratorCompletion &completion;
                bool finished = false;
                ~AbortIfStillGenerating() {
                    if (!finished) {
                        (void)completion.abort();
                    }
                }
            } guard{completion};

            while (hailort::genai::LLMGeneratorCompletion::Status::GENERATING ==
                   completion.generation_status()) {
#ifdef HAILO_GENAI_LLM_HAS_LONG_READ_TIMEOUT
                auto tok_exp = completion.read(kTokenReadTimeout);
#else
                auto tok_exp = completion.read();
#endif
                if (!tok_exp) {
                    throw std::runtime_error(
                        status_msg("LLMGeneratorCompletion::read failed",
                                   tok_exp.status()));
                }
                std::string token = tok_exp.release();
                full_text += token;

                if (!token_callback.is_none() || !should_stop.is_none()) {
                    py::gil_scoped_acquire acquire_gil;
                    if (!token_callback.is_none()) {
                        token_callback(token);
                    }
                    if (!should_stop.is_none() && py::cast<bool>(should_stop())) {
                        stopped_early = true;
                    }
                }
                if (stopped_early) {
                    auto abort_status = completion.abort();
                    guard.finished = true;
                    if (HAILO_SUCCESS != abort_status) {
                        throw std::runtime_error(
                            status_msg("LLMGeneratorCompletion::abort failed",
                                       abort_status));
                    }
                    break;
                }
            }
            guard.finished = true;
        }
        return full_text;
    }

    void clear_context() {
        std::lock_guard<std::mutex> generate_lock(generate_mu_);
        auto status = llm_->clear_context();
        if (HAILO_SUCCESS != status) {
            throw std::runtime_error(status_msg("clear_context failed", status));
        }
    }

    size_t context_usage_size() {
        auto exp = llm_->get_context_usage_size();
        return exp ? exp.release() : 0;
    }

    size_t max_context_capacity() {
        auto exp = llm_->max_context_capacity();
        return exp ? exp.release() : 0;
    }

    // Drop GenAI LLM before constructing another HEF. Hailo-10H has one KV-cache.
    void release() {
        std::lock_guard<std::mutex> generate_lock(generate_mu_);
        llm_.reset();
        vdevice_.reset();
    }

private:
    std::string group_id_;
    std::shared_ptr<hailort::VDevice> vdevice_;
    std::shared_ptr<hailort::genai::LLM> llm_;
    std::mutex generate_mu_;
    float temperature_;
    uint32_t seed_;
    uint32_t default_max_tokens_;
};

PYBIND11_MODULE(hailo_llm_cpp, m) {
    m.doc() = "Native hailort::genai::LLM wrapper with explicit GIL release "
              "around the blocking decode loop. API shape follows the HailoRT "
              "headers this .so was compiled against (5.1 generate vs 5.2+ tools).";
    m.attr("compiled_hailort_version") = kCompiledHailoRT;
#ifdef HAILO_GENAI_LLM_HAS_TOOLS
    m.attr("has_native_tools") = true;
#else
    m.attr("has_native_tools") = false;
#endif
#ifdef HAILO_GENAI_LLM_HAS_LONG_READ_TIMEOUT
    m.attr("has_long_read_timeout") = true;
#else
    m.attr("has_long_read_timeout") = false;
#endif
    py::class_<HailoLLMCpp>(m, "HailoLLMCpp")
        .def(py::init<const std::string &, float, uint32_t, uint32_t, const std::string &>(),
             py::arg("hef_path"), py::arg("temperature") = 0.15f,
             py::arg("seed") = 42, py::arg("max_tokens") = 24,
             py::arg("group_id") = "SHARED")
        .def("generate", &HailoLLMCpp::generate, py::arg("prompt_json_strings"),
             py::arg("max_tokens") = 0, py::arg("token_callback") = py::none(),
             py::arg("should_stop") = py::none(),
             py::arg("tools_json_strings") = std::vector<std::string>{})
        .def("clear_context", &HailoLLMCpp::clear_context)
        .def("release", &HailoLLMCpp::release)
        .def("context_usage_size", &HailoLLMCpp::context_usage_size)
        .def("max_context_capacity", &HailoLLMCpp::max_context_capacity);
}

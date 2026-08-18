// Opt-in native LLM backend: wraps hailort::genai::LLM directly (not
// hailo_platform.genai's Python binding) so the blocking NPU read loop can
// explicitly release the GIL, letting the Python-side TTS worker thread
// actually get scheduled during decode. See ROADMAP.md #6d / Sprint 1b.
//
// JSON message serialization stays in Python (json.dumps per message) --
// this file only ever sees pre-built JSON strings, so it needs no JSON
// library of its own.
#include <pybind11/pybind11.h>
#include <pybind11/stl.h>

#include <memory>
#include <stdexcept>
#include <string>
#include <vector>

#include "hailo/genai/llm/llm.hpp"
#include "hailo/vdevice.hpp"

namespace py = pybind11;

class HailoLLMCpp {
public:
    // group_id must match pipeline.py's SHARED_VDEVICE_GROUP_ID (params.group_id
    // there) -- on a single Hailo-10H, a VDevice created without joining that
    // same group requests an exclusive physical device and fails with
    // HAILO_OUT_OF_PHYSICAL_DEVICES once the Python-side VDevice already holds
    // the one available device.
    HailoLLMCpp(const std::string &hef_path, float temperature, uint32_t seed,
                uint32_t max_tokens, const std::string &group_id) {
        hailo_vdevice_params_t params;
        hailo_status init_status = hailo_init_vdevice_params(&params);
        if (HAILO_SUCCESS != init_status) {
            throw std::runtime_error("hailo_init_vdevice_params failed, status=" +
                                      std::to_string(static_cast<int>(init_status)));
        }
        params.group_id = group_id.c_str();

        auto vdevice_exp = hailort::VDevice::create_shared(params);
        if (!vdevice_exp) {
            throw std::runtime_error(
                "VDevice::create_shared failed, status=" +
                std::to_string(static_cast<int>(vdevice_exp.status())));
        }
        vdevice_ = vdevice_exp.release();

        hailort::genai::LLMParams llm_params(hef_path, "", true);
        auto llm_exp = hailort::genai::LLM::create(vdevice_, llm_params);
        if (!llm_exp) {
            throw std::runtime_error(
                "LLM::create failed, status=" +
                std::to_string(static_cast<int>(llm_exp.status())));
        }
        llm_ = std::make_shared<hailort::genai::LLM>(llm_exp.release());

        temperature_ = temperature;
        seed_ = seed;
        default_max_tokens_ = max_tokens;
    }

    // Runs the full generate+read loop with the GIL released; re-acquires it
    // only for the duration of each token_callback invocation. Returns the
    // full generated text (already token_callback-visited piece by piece).
    std::string generate(const std::vector<std::string> &prompt_json_strings,
                          uint32_t max_tokens, py::object token_callback,
                          py::object should_stop) {
        auto params_exp = llm_->create_generator_params();
        if (!params_exp) {
            throw std::runtime_error("create_generator_params failed");
        }
        auto params = params_exp.release();
        params.set_temperature(temperature_);
        params.set_seed(seed_);
        params.set_max_generated_tokens(max_tokens > 0 ? max_tokens : default_max_tokens_);

        std::string full_text;
        bool stopped_early = false;

        {
            py::gil_scoped_release release_gil;

            auto completion_exp = llm_->generate(params, prompt_json_strings);
            if (!completion_exp) {
                throw std::runtime_error("LLM::generate failed");
            }
            auto completion = completion_exp.release();

            while (hailort::genai::LLMGeneratorCompletion::Status::GENERATING ==
                   completion.generation_status()) {
                auto tok_exp = completion.read();
                if (!tok_exp) {
                    break;
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
                    completion.abort();
                    break;
                }
            }
        }
        return full_text;
    }

    void clear_context() {
        auto status = llm_->clear_context();
        if (HAILO_SUCCESS != status) {
            throw std::runtime_error("clear_context failed, status=" +
                                      std::to_string(static_cast<int>(status)));
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

private:
    std::shared_ptr<hailort::VDevice> vdevice_;
    std::shared_ptr<hailort::genai::LLM> llm_;
    float temperature_;
    uint32_t seed_;
    uint32_t default_max_tokens_;
};

PYBIND11_MODULE(hailo_llm_cpp, m) {
    m.doc() = "Native hailort::genai::LLM wrapper with explicit GIL release "
              "around the blocking decode loop.";
    py::class_<HailoLLMCpp>(m, "HailoLLMCpp")
        .def(py::init<const std::string &, float, uint32_t, uint32_t, const std::string &>(),
             py::arg("hef_path"), py::arg("temperature") = 0.15f,
             py::arg("seed") = 42, py::arg("max_tokens") = 24,
             py::arg("group_id") = "SHARED")
        .def("generate", &HailoLLMCpp::generate, py::arg("prompt_json_strings"),
             py::arg("max_tokens") = 0, py::arg("token_callback") = py::none(),
             py::arg("should_stop") = py::none())
        .def("clear_context", &HailoLLMCpp::clear_context)
        .def("context_usage_size", &HailoLLMCpp::context_usage_size)
        .def("max_context_capacity", &HailoLLMCpp::max_context_capacity);
}

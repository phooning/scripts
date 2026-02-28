function rffmpeg --description "Recursively multithread convert media files using ffmpeg."
    argparse h/help 'p/preset=' -- $argv
    or return 1

    # TODO: Specify output directory.
    if set -q _flag_help
        echo "Usage: rffmpeg [options] <input_ext> <output_ext>"
        echo "Options:"
        echo "  -p, --preset <name>     Encoder preset: web | nvidia | apple"
        echo "                            web    - H.264 (libx264), optimized for streaming (+faststart)"
        echo "                            nvidia - AV1 via av1_nvenc, fallback to libx264"
        echo "                            apple  - H.265 via hevc_videotoolbox, fallback to libx265"
        return 0
    end

    if test (count $argv) -ne 2
        echo "Error: Please provide both input and output formats."
        echo "Example: rffmpeg webm mp4"
        return 1
    end

    # Sanitize extensions to lowercase
    set -l input_ext (string lower $argv[1])
    set -l output_ext (string lower $argv[2])

    set -l total_found 0
    set -l success_count 0
    set -l fail_count 0
    set -l skipped_count 0

    # Helper: check if an encoder is available in this ffmpeg build
    function __ffmpeg_has_encoder
        ffmpeg -hide_banner -encoders 2>/dev/null | grep -q "^ ...... $argv[1]"
    end

    # Helper: check if two codec families match (for stream-copy eligibility)
    # Returns 0 (true) if the input codec is copy-compatible with the output container
    function __codec_is_copy_compatible
        set -l in_ext $argv[1]
        set -l out_ext $argv[2]
        # HEVC source into HEVC-friendly containers
        if string match -q -r '^(hevc|h265)$' -- $in_ext
            and string match -q -r '^(mkv|mp4|mov)$' -- $out_ext
            return 0
        end
        # AV1 source into AV1-friendly containers
        if string match -q -r '^(av1)$' -- $in_ext
            and string match -q -r '^(mkv|mp4|webm)$' -- $out_ext
            return 0
        end
        # H.264 source into H.264-friendly containers
        if string match -q -r '^(h264|x264)$' -- $in_ext
            and string match -q -r '^(mkv|mp4|mov)$' -- $out_ext
            return 0
        end
        return 1
    end

    # -- Resolve preset flags --
    set -l preset_ffmpeg_flags
    if set -q _flag_preset
        switch "$_flag_preset"
            case web
                set preset_ffmpeg_flags \
                    -c:v libx264 -preset slow -profile:v high -level 4.1 \
                    -crf 20 -c:a aac -b:a 192k -movflags +faststart

            case nvidia
                if __ffmpeg_has_encoder av1_nvenc
                    set preset_ffmpeg_flags \
                        -c:v av1_nvenc -preset p7 -rc vbr -cq 10 \
                        -c:a libopus -b:a 192k -movflags +faststart
                else
                    echo "[WARN] av1_nvenc not available. Falling back to libx264."
                    set preset_ffmpeg_flags \
                        -c:v libx264 -preset slow -profile:v high -level 4.1 \
                        -crf 20 -c:a aac -b:a 192k -movflags +faststart
                end

            case apple
                if __ffmpeg_has_encoder hevc_videotoolbox
                    set preset_ffmpeg_flags \
                        -c:v hevc_videotoolbox -q:v 60 -tag:v hvc1 \
                        -c:a aac -b:a 192k -movflags +faststart
                else
                    echo "[WARN] hevc_videotoolbox not available. Falling back to libx265."
                    set preset_ffmpeg_flags \
                        -c:v libx265 -preset slow -crf 24 -tag:v hvc1 \
                        -c:a aac -b:a 192k -movflags +faststart
                end

            case '*'
                echo "Error: Unknown preset '$_flag_preset'. Valid options: web | nvidia | apple"
                return 1
        end
    end

    echo "=============================================================================="
    if set -q _flag_preset
        echo "Preset: $_flag_preset"
    end
    echo "Starting recursive conversion from .$input_ext to .$output_ext ..."
    echo "Directory: "(pwd)
    echo "=============================================================================="

    set -l ffmpeg_flags

    if set -q _flag_preset; and string match -q -r '^(mp4|mkv|mov|webm)$' -- $output_ext
        set ffmpeg_flags $preset_ffmpeg_flags
    else
        switch "$input_ext->$output_ext"
            # -- VIDEO: H.264 targets --
            case "webm->mp4" "mkv->mp4" "avi->mp4" "mov->mp4" "hevc->mp4" "h265->mp4" "av1->mp4"
                set ffmpeg_flags \
                    -c:v libx264 -preset slow -crf 23 \
                    -c:a aac -b:a 128k -movflags +faststart

                # -- VIDEO: H.265/HEVC targets --
            case "mp4->mkv" "webm->mkv" "avi->mkv" "mov->mkv" "av1->mkv"
                # Stream-copy if source is already HEVC, otherwise encode
                # (handled dynamically below via __codec_is_copy_compatible)
                set ffmpeg_flags \
                    -c:v libx265 -preset slow -crf 24 \
                    -c:a aac -b:a 128k
            case "hevc->mkv" "h265->mkv"
                set ffmpeg_flags -c:v copy -c:a copy
            case "hevc->mp4" "h265->mp4"
                set ffmpeg_flags -c:v copy -c:a copy -movflags +faststart

                # -- VIDEO: AV1 targets --
            case "mp4->webm" "mkv->webm" "mov->webm"
                set ffmpeg_flags -c:v libsvtav1 -crf 30 -c:a libopus -b:a 128k
            case "av1->webm" "av1->mkv" "av1->mp4"
                set ffmpeg_flags -c:v copy -c:a copy

                # -- VIDEO: GIF --
            case "mp4->gif" "webm->gif" "mkv->gif"
                set ffmpeg_flags -vf 'fps=15,scale=480:-1:flags=lanczos'

                # -- AUDIO --
            case "mp3->wav" "flac->wav" "ogg->wav" "m4a->wav"
                set ffmpeg_flags -c:a pcm_s16le
            case "wav->mp3" "flac->mp3" "ogg->mp3" "m4a->mp3"
                set ffmpeg_flags -c:a libmp3lame -q:a 2
            case "wav->flac" "mp3->flac" "ogg->flac" "m4a->flac"
                set ffmpeg_flags -c:a flac

                # -- DEFAULT --
            case '*'
                echo "[INFO] No specific flags for .$input_ext -> .$output_ext."
                if __codec_is_copy_compatible $input_ext $output_ext
                    echo "[INFO] Codec appears copy-compatible with target container. Using stream copy."
                    set ffmpeg_flags -c:v copy -c:a copy
                else if string match -q -r '^(mp3|wav|flac|ogg|aac|m4a)$' -- $output_ext
                    set ffmpeg_flags -vn
                else if string match -q -r '^(mp4|mkv|mov|webm|avi)$' -- $output_ext
                    set ffmpeg_flags -c:v libx264 -c:a aac
                end
        end
    end

    # Set statistics for total found and skipped files.
    set total_found (fd -e "$input_ext" | count)
    for file in (fd -e "$input_ext")
        set output_file (string replace -r "\.$input_ext\$" "\.$output_ext" (string lower $file))

        if test -f "$output_file"
            set skipped_count (math $skipped_count + 1)
            echo "[SKIP] '$output_file' already exists."
        end
    end

    echo "Total $input_ext input files: $total_found"
    echo "Total skipped files: $skipped_count"

    if test $total_found -eq $skipped_count
        echo "No files to process."
        return 0
    end

    set -l ffmpeg_flags_str (string join -- " " $ffmpeg_flags)
    echo "Running ffmpeg preset $_flag_preset; flags: $ffmpeg_flags"

    fd -e "$input_ext" -0 | parallel -0 --progress --eta \
        "test -f '{.}.$output_ext' || ffmpeg -hide_banner -loglevel error -stats -i {} $ffmpeg_flags_str '{.}.$output_ext'"
end

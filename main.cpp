#include <Arduino.h>
#include <Picossci_NTSC.h>
#include "SdFat.h"

// SDカードのSPI設定。
#define SD_SPI SPI1

const uint8_t SD_CS_PIN = 13;
const uint8_t SPI1_MISO = 12;
const uint8_t SPI1_MOSI = 11;
const uint8_t SPI1_SCK  = 10;

// 192x144
// 解像度はこれが限界で、これ以上あげるとSDカードやメモリの性能不足により映像が乱れます。
static constexpr const uint8_t frame_buf_width = 192;
static constexpr const uint8_t frame_buf_height = 144;
static const int total_frame = 6566; 
static const int audio_total_sector = 75504;

// 描画中もSDから読み込めるようにバッファを2つ用意
static uint8_t buffer[2][frame_buf_width * frame_buf_height / 2];
static uint8_t line_buffer[frame_buf_width]; // 1行分の展開用
static uint8_t display_idx = 0; // いまどっちのバッファを表示してるか
static int frame_num = 0;
static volatile bool vsync_flag = false;
static uint32_t audio_sector = 0;

static Picossci_NTSC picossci_ntsc;
static SdCardFactory cardFactory;
static SdCard* card = nullptr;

// Bad Appleのパレットです。ほかの動画を流したい場合は変更する必要があります。最大16色までです。
static const uint32_t colors[] = {
  0x000000, 0x010101, 0x030203, 0x0c090c, 0x171517,
  0x252325, 0x393639, 0x525052, 0x727072, 0x979597,
  0xbebcbe, 0xeae8ea, 0xfdfcfd, 0xfffdff, 0xffffff
};

// ビデオデータのコピー。
static void copy_frame(void) {
  if (!card) return;

  uint32_t sector_offset = (uint32_t)frame_num * (frame_buf_width * frame_buf_height / 2 / 512);
  uint8_t next_idx = (display_idx + 1) % 2;

  if (card->readSectors(sector_offset, (uint8_t*)buffer[next_idx], frame_buf_width * frame_buf_height / 2 / 512)) {
    display_idx = next_idx;
    frame_num++;
    if (frame_num >= total_frame) {
      frame_num = 0; // ループ再生
    }
  }
}

// 音声データをコピー。
static void fill_audio() {
  if (!card) return;

  // ビデオデータの後ろにデータがある想定
  int video_total_sector = total_frame * (frame_buf_width * frame_buf_height / 2 / 512);
  
  while (picossci_ntsc.audio.availableForWrite() >= 512) {
    int16_t audio_buf[256];
    if (card->readSectors(audio_sector + video_total_sector, (uint8_t*)audio_buf, 1)) {
      picossci_ntsc.audio.write(audio_buf, 512);
      audio_sector++;
      if (audio_sector >= audio_total_sector) audio_sector = 0;
    } else {
      break;
    }
  }
}

// ビデオのコールバック
static void callback_video(void*) {
  for (;;) {
    int y = picossci_ntsc.video.getCurrentY();
    if (y < 0) return;

    // 1フレーム書き終わったらフラグを立てる
    if (y == 479) {
      vsync_flag = true;
    }

    // 144行の元データを480行に引き伸ばしてスキャン
    int src_y = (y * frame_buf_height) / 480;
    uint8_t* src_line = &buffer[display_idx][src_y * (frame_buf_width / 2)];

    // 少しでもSDカードの読み込み時間を減らすために、一バイトに二ピクセル分のデータを入れています。
    for (int x = 0; x < frame_buf_width / 2; x++) {
      uint8_t packed_byte = src_line[x];
      line_buffer[x * 2] = (packed_byte >> 4) & 0x0F;
      line_buffer[x * 2 + 1] = packed_byte & 0x0F;
    }

    picossci_ntsc.video.writeScanLine(line_buffer, frame_buf_width);
  }
}

void setup(void) {
  picossci_ntsc.setCpuClock(157500);

  // SDの設定
  SD_SPI.setSCK(SPI1_SCK);
  SD_SPI.setRX(SPI1_MISO);
  SD_SPI.setTX(SPI1_MOSI);
  static SdSpiConfig spiConfig(SD_CS_PIN, SHARED_SPI, SD_SCK_MHZ(26.25), &SD_SPI);
  card = cardFactory.newCard(spiConfig);

  copy_frame(); // 最初の1フレームをロード
  
  auto cfg = picossci_ntsc.video.getConfig();
  cfg.callback_function = callback_video;
  cfg.dma_buf_count = 16;
  
  picossci_ntsc.video.setOffset(0);
  picossci_ntsc.video.setScale(((720) * 256) / frame_buf_width); // 横720ピクセル相当に拡大
  picossci_ntsc.video.setPixelMode(Picossci_NTSC::pixel_mode_t::pixel_palette);
  
  picossci_ntsc.video.init(cfg);
  
  // パレットの登録。16色目以降は白で埋める。
  int num_colors = sizeof(colors) / sizeof(colors[0]);
  for(int i = 0; i < 256; i++) {
    picossci_ntsc.video.setPalette(i, (i < num_colors) ? colors[i] : 0xffffff);
  }

  picossci_ntsc.video.start();

  // 44100khz, 16bit
  auto audio_cfg = picossci_ntsc.audio.getConfig();
  audio_cfg.freq_hz = 44100;
  audio_cfg.dma_buf_size = 192;
  audio_cfg.dma_buf_count = 16;
  picossci_ntsc.audio.init(audio_cfg);
  picossci_ntsc.audio.start();
}

void loop() {
  // フラグが立つと次のフレームを読む
  if (vsync_flag) {
    vsync_flag = false;
    copy_frame();
  }

  fill_audio();
}
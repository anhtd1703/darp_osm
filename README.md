# DARP xe ghép hai chiều TB–HN

## Mục đích và giới hạn
Mô phỏng lịch 04:00–18:00 (15 chuyến theo kế hoạch mỗi chiều), 6 xe (3 ở mỗi thành phố), 6 khách/xe. Mỗi yêu cầu 1 khách, biết trước giờ xuất phát, 1 điểm đón và 1 điểm trả. Mỗi slot có tối đa một xe mỗi chiều; không chuyển yêu cầu bị từ chối sang giờ sau. Xe đến depot đích mới được chuyển chiều, và sau turnaround mới sẵn sàng. Nếu không có khách phù hợp, xe vẫn chạy rỗng để duy trì luân chuyển. Không có xe rảnh thì hủy chuyến; tất cả khách của chuyến đó không được phục vụ. Đây là giả định thí nghiệm, không phải vận hành đã khảo sát của nhà xe.

Giới hạn 180 phút tính từ lúc xe rời depot đến lúc tới depot đích (gồm gom khách, chạy đường dài, trả khách, thời gian chờ và dịch vụ); nghỉ sau đó tính riêng. Xe chạy rỗng vẫn được ghi nhận nếu vượt 180 phút và thời điểm sẵn sàng thực tế bị dời. Thời gian xe/ca không phải thời gian làm việc hợp pháp của một tài xế. Chưa mô hình hóa đổi tài xế, nghỉ bắt buộc, yêu cầu động, khách hủy/đến trễ hoặc tắc đường theo đoạn. Thuật toán tối ưu từng slot, chưa tối ưu toàn cục lịch đội xe.

## 1. Cài đặt (Docker Desktop + Compose v2)
Chạy từng dòng ở thư mục chứa `compose.yaml`. Có thể dùng PowerShell, Bash hoặc terminal VS Code. Cần mạng khi pull image, tải PBF và cài package. Nên cấp Docker khoảng 8 GB RAM trở lên nếu có, và dự phòng ít nhất 10 GB đĩa; đây là khuyến nghị, không phải yêu cầu chính xác. Nếu xử lý toàn quốc quá nặng, có thể cắt vùng chứa cả Hà Nội, Thái Bình và tất cả hành lang kết nối bằng osmium trước khi extract; cập nhật đường dẫn nhất quán.

```bash
docker compose --profile setup run --rm download
docker compose --profile setup run --rm prepare osrm-extract -p /opt/car.lua /data/vietnam-latest.osm.pbf
docker compose --profile setup run --rm prepare osrm-partition /data/vietnam-latest.osrm
docker compose --profile setup run --rm prepare osrm-customize /data/vietnam-latest.osrm
docker compose up -d osrm
docker compose logs osrm
docker compose build app
```
OSRM dùng MLD; phiên bản hiện hành hỗ trợ Table với `annotations=distance,duration`. Kiểm tra thử sau khi dịch vụ sẵn sàng (PowerShell dùng `curl.exe`):
```bash
curl "http://localhost:5000/table/v1/driving/106.344,20.445;105.844,21.005?annotations=distance,duration"
```
Mong muốn `code: Ok` và ma trận 2x2. Nếu lỗi `NoSegment` hoặc `NoTable`, kiểm tra đường dẫn, dữ liệu, profile và vị trí; không tự động thay bằng đường thẳng. `docker compose logs osrm` để xem lỗi. Lưu digest image bằng `docker image inspect ghcr.io/project-osrm/osrm-backend:latest` và snapshot PBF để tái lập. Không chạy tải và extract lại nếu giữ snapshot cũ. Xóa `data/matrix_cache` nếu đổi profile, thuật toán routing hoặc graph (cache đã có khóa SHA256 PBF nhưng không khóa toàn bộ cấu hình engine).

## 2. Lấy địa điểm thực từ PBF
```bash
docker compose run --rm app python src/main.py extract
```
Tệp `data/poi_pool.csv`: điểm OSM node có tên, thuộc amenity/shop/tourism/leisure/office, không phải địa chỉ cá nhân. Lấy 120 điểm mỗi thành phố nếu đủ; phân bổ vòng tròn theo zone để hạn chế tập trung; loại tọa độ snap trùng, xa đường ô tô quá 250m; lưu tọa độ gốc và tọa độ snap. Các depot trong config là giả định, không phải bến xe xác nhận. Địa chỉ lấy từ `addr:*`; nếu thiếu thì dùng tên POI và đánh dấu `address_source=POI name only`, không tự bịa địa chỉ.

Mặc định Thái Bình là bbox khu vực thành phố cũ; Hà Nội là 12 vùng gần tâm các quận cũ (bán kính ước lượng, **không phải ranh giới hành chính**), có thể bỏ sót hoặc lẫn khu vực ngoại thành. Để lọc chính xác, cung cấp `data/areas.geojson` dạng FeatureCollection WGS84, mỗi feature Polygon/MultiPolygon có `properties.city` là `TB` hoặc `HN`, và `properties.name` là tên vùng/quận. Nên dùng ranh giới lịch sử có nguồn và ngày nếu muốn đúng phạm vi quận trước sắp xếp hành chính. Khi file này tồn tại, bộ lọc polygon thay thế hoàn toàn các bbox/vùng gần đúng. Cần tự cung cấp polygon có nguồn, không có sẵn trong gói. Sau khi chỉnh vùng, chạy lại extract. Lưu `data/osm_manifest.json` chứa hash PBF và thông tin lọc.

## 3. Sinh dữ liệu
```bash
docker compose run --rm app python src/main.py generate --scenarios low normal peak overload --seeds 11 22 33
```
`data/requests_osrm.csv` một dòng mỗi khách, `data/stops_osrm.csv` hai dòng mỗi khách (pickup/dropoff). Thời gian là số giây từ 00:00 theo giờ Việt Nam, ví dụ 04:00 = 14400. `booked_sec` là thời điểm đặt trước chuyến 30–240 phút; `slot_sec` là giờ xe rời depot; pickup sớm nhất nằm trong 0–25 phút kể từ slot, muộn nhất 10 phút sau đó (không quá 45 phút từ slot). Hạn trả khách = slot + 180 phút. Thời gian dừng tại pickup 120s, dropoff 60s. Thời gian ngồi xe tính từ hoàn tất pickup đến bắt đầu dropoff, không quá thời gian trực tiếp + 45 phút. Chỉnh config để thử cửa sổ rộng hơn, chi phí, số khách, điểm depot, v.v. Nhu cầu mặc định là số lượng cố định theo profile từng giờ; `--jitter 1` cộng nhiễu nguyên ngẫu nhiên -1..+1 có seed (không dưới 0). Mỗi seed chọn POI và khung giờ khác. Tất cả địa điểm là đại diện điểm đón/trả, không khẳng định có người đặt xe thật.

## 4. Chạy benchmark
```bash
docker compose run --rm app python src/main.py benchmark --scenarios normal --seeds 11 --output results/first

docker compose run --rm app python src/main.py benchmark --scenarios low normal peak overload --seeds 11 22 33 44 55 --output results/base

docker compose run --rm app python src/main.py benchmark --scenarios normal peak --seeds 11 22 33 --traffic 1 1.15 1.3 --turnaround 0 15 --output results/sensitivity

docker compose run --rm app python src/main.py benchmark --scenarios normal peak --seeds 11 22 33 --turnaround 15 --fleet-per-city 4 --output results/fleet8
```
Chạy thử đầu tiên trước khi chạy batch. `solver_seconds=1` là giới hạn tìm kiếm mỗi chuyến; có thể tăng để đánh giá chất lượng/chi phí tính toán, không chứng minh tối ưu toàn cục. `traffic` nhân toàn bộ ma trận thời gian, không phải dữ liệu giao thông thực. Dùng cùng seed, pool và ma trận cho hai phương án. Benchmark tự sinh lại cùng bộ yêu cầu cho từng seed theo config và xuất bản sao vào thư mục results; không đọc ngược file generate (muốn thay bằng dữ liệu thu thập thì sửa loader). Không so trực tiếp runtime tổng giữa cold cache/warm cache; tách `solver_ms` và `matrix_ms`, nếu đo API hãy xóa cache trước mỗi phương án hoặc làm phép đo độc lập.

AS-IS: FCFS theo giờ đặt, chèn yêu cầu vào cuối danh sách đón và cuối danh sách trả, kiểm tra cứng sức chứa, đón trước trả, khung giờ, giới hạn hành trình và đi vòng. Không vừa trả vừa gom khách chiều ngược lại. Nếu một khách làm tuyến không khả thi, bỏ qua và xét khách tiếp theo.

TO-BE: OR-Tools RoutingModel, one vehicle per slot, Time/Load dimensions, cặp pickup/delivery cùng active và cùng xe, cấm arc dropoff→pickup, ràng buộc ride-time conditional. Hàm mục tiêu phạt bỏ một yêu cầu bằng (số node + 1) * max khoảng cách cung + 1, ưu tiên phục vụ nhiều khách nhất sau đó giảm quãng đường (đối với mỗi slot). Kiểm tra nghiệm độc lập bằng evaluate; nếu không có nghiệm hoặc nghiệm tệ hơn greedy theo cặp (-số khách phục vụ, quãng đường), fallback và ghi trạng thái. Do lịch luân chuyển phụ thuộc thời điểm xe đến, TO-BE không được đảm bảo tốt hơn toàn ngày. Đây là baseline giả định, không phải quy trình quan sát thực tế.

## 5. Hiển thị tuyến trên bản đồ
```bash
docker compose run --rm app python src/main.py map --routes results/first/routes.json --scenario normal --seed 11 --hour 7 --direction TB-HN --method ortools --output results/map_tobe.html
docker compose run --rm app python src/main.py map --routes results/first/routes.json --scenario normal --seed 11 --hour 7 --direction TB-HN --method greedy --output results/map_asis.html
```
Mở HTML trong trình duyệt; nền OSM cần Internet. Đường đi được truy vấn OSRM Route API, điểm đón xanh lam, trả xanh lá, depot đầu đen, depot cuối đỏ. Tuyến đi qua các tọa độ snap, hình học Route API có thể khác nhẹ ma trận từng cặp. Có thể thêm `--traffic` và `--turnaround` để chọn chính xác một kịch bản. Nếu chuyến không xuất phát, không có route để vẽ.

## 6. Kết quả
- `summary.csv`: KPI theo ngày/seed/kịch bản/phương pháp, gồm số phục vụ, số từ chối, tỷ lệ lấp đầy, chuyến bị hủy, km, chi phí, chi phí/khách, thời gian đi vòng và P90, thời gian tính toán.
- `paired_comparison.csv`: từng seed, delta TO-BE - AS-IS (chỉ có khi chạy cả 2).
- `trips.csv`: lịch xe theo giờ, điểm đến gián tiếp qua direction, thời điểm đến và sẵn sàng.
- `passengers.csv`: trạng thái từng yêu cầu, thời gian đón/trả, ride/direct/detour.
- `routes.json`: điểm dừng theo thứ tự, thời gian phục vụ.
- `requests.csv`, `stops.csv`, `pool.csv`, `config_used.json`: đầu vào để audit.

Tỷ lệ đúng giờ trên khách đã phục vụ = 100% theo ràng buộc cứng; chỉ số có ý nghĩa phân biệt là tỷ lệ khách được phục vụ đúng giờ trên tổng yêu cầu và độ trễ/P90. Thời gian đi vòng so với tuyến trực tiếp = max(0, ride_time - direct_time). Chi phí = 4500 VND/km (giả định) + chi phí cố định/chuyến (mặc định 0). Đừng kết luận chi phí thấp là tốt nếu phục vụ ít hơn; so thêm chi phí/khách và tỷ lệ phục vụ. So sánh theo paired seed, báo cáo trung bình ± độ lệch chuẩn, thêm p90 và số chuyến hủy; khi so thời gian đi vòng, có thể tính trên nhóm khách cùng được phục vụ ở hai phương án để tránh selection bias. `summary.csv` P90 dùng nearest rank.

## 7. Test không cần OSM
```bash
docker compose run --rm --no-deps app python -m unittest discover -s tests -v
docker compose run --rm --no-deps app python src/main.py benchmark --synthetic --scenarios normal --seeds 11 --output results/offline
```
`--synthetic` sinh địa điểm giả và ma trận đường thẳng x 1.25 / tốc độ 55km/h; chỉ dùng kiểm tra chương trình, không phải kết quả nghiên cứu trên đường thật. `results/smoke` là lần chạy Greedy offline trong môi trường soạn mã (7 test pass, 1 test OR-Tools skip vì chưa có thư viện). Chưa chạy Docker, trích PBF, OSRM hoặc OR-Tools trong môi trường soạn mã, cần xác minh trên máy có Docker. Tệp `data/requests_synthetic.csv` và `data/stops_synthetic.csv` là mẫu schema, không phải POI thật.

## Tài liệu
- https://download.geofabrik.de/asia/vietnam.html
- https://github.com/Project-OSRM/osrm-backend
- https://github.com/Project-OSRM/osrm-backend/blob/master/docs/http.md
- https://developers.google.com/optimization/routing/pickup_delivery
- https://developers.google.com/optimization/routing/vrptw
- https://www.openstreetmap.org/copyright (ghi công OpenStreetMap contributors, tuân thủ ODbL)

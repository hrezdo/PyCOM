import network
from network import LoRa
import binascii
import time
import pycom
import socket
import struct
import json
import machine
from machine import Pin
import urequests
import gc
import crypto

VERSION = "v0.1 on 2017-03-21"

#SHT31 constants
SHT31_ADDR = 68
SHT31_STATUS_REGISTER = b'\xf3\x2d'
PERIODIC_MEASUREMENT_COMMAND = b'\x27\x37'

#Lora constants
_LORA_PKG_FORMAT = "!BBBBBBBBBBBBBBB"
OP_MODES = ["LoRa gateway", "Temperature & Humidity sensor", "Pulse counter"]
BANDWIDTH = ["125", "250"]
POWER_TX = ["2", "4", "6", "8", "10", "12", "14"]
CODING_RATE = ["4/5", "4/6", "4/7", "4/8"]
SPREADING_FACTOR = ["7", "8", "9", "10", "11", "12"]

pycom.heartbeat(False)

i2c = machine.I2C(0, machine.I2C.MASTER, baudrate=100000, pins=('P8', 'P9'))
rtc = machine.RTC()

data=""
response=""

class LoRaNode():
	def __init__(self, i2c, rtc):

		self.now = 0
		self.lastReportTime = 0

		self.ID = machine.unique_id()
		self.IDhex = str(binascii.hexlify(machine.unique_id()))[2:-1]

		self.i2c = i2c
		self.i2cBuffer = bytearray(2)
		self.SHT31buffer = bytearray(6)
		#self.uart2 = uart2
		self.rtc = rtc
		self.currentTimeDate = self.rtc.now()

		self.lora = None
		self.lora_sock = None

		self.voltage = 100
		self.distance = 0

		self.WiFiIsConnected = False
		self.WiFiIsConnectedLast = False

		self.config = None
		self.loadConfig()
		self.authBase64 = str(binascii.b2a_base64("%s:%s" % (self.config['deviceLogin'], self.config['devicePassword'])))[2:-3]
		self.authBase64default = str(binascii.b2a_base64("%s:%s" % ("devicebootstrap", "Fhdt1bb1f")))[2:-3]
		self.rebootSW = Pin('P10', mode=Pin.IN, pull=Pin.PULL_UP)


	def rebootSWcallback(self, arg):
		if self.config["bootToConfigMode"] == 0:
			self.config["bootToConfigMode"] = 1
			self.p("WARNING", "Rebooting to configuration mode...")
		else:
			self.config["bootToConfigMode"] = 0
			self.p("WARNING", "Rebooting to operational mode...")
		self.saveConfig()
		machine.reset()


	def p(self, logItemSeverity, message):
		self.currentTimeDate = self.rtc.now()
		if (logItemSeverity == "ERROR" and self.config['serialVerbose'] > 0) or (logItemSeverity == "WARNING" and self.config['serialVerbose'] > 0) or (logItemSeverity == "INFO" and self.config['serialVerbose'] > 1) or ((logItemSeverity == "LORA" or logItemSeverity == "WAN"or logItemSeverity == "CoT") and self.config['serialVerbose'] > 2):
			print("%sB [%s-%02d-%02d %02d:%02d:%02d] %s: %s" %(gc.mem_free(), self.currentTimeDate[0],self.currentTimeDate[1],self.currentTimeDate[2],self.currentTimeDate[3],self.currentTimeDate[4],self.currentTimeDate[5],logItemSeverity,message))

	def loadConfig(self):
		try:
			configFile = open('/flash/nodeConfig.json', 'r')
			data = configFile.readall()
			self.config = json.loads(data)
			configFile.close()
			del data
			del configFile
			self.p("INFO", "Configuration loaded")
		except Exception as e:
			self.p("ERROR", "Loading of configuration failed")

	def saveConfig(self):
		configFile = open('/flash/nodeConfig.json', 'w')
		configFile.write(json.dumps(self.config))
		configFile.close()
		self.p("INFO", "Config updated")

	def rebootToConfigMode(self):
		self.config["bootToConfigMode"] = 1
		self.saveConfig()
		machine.reset()

	def run(self):
		self.p("INFO", "Device ID: %s" % (self.IDhex))
		swVersion = os.uname()
		self.p("INFO", "Firmware version: %s" % (swVersion[3]))
		del swVersion
		self.p("INFO", "Software version: %s" % (VERSION))
		self.p("INFO", "Auth string: %s" % (self.authBase64))

		#self.rebootSW.callback(Pin.IRQ_FALLING, self.rebootSWcallback)
		if self.config["bootToConfigMode"]:
			self.configMode()
		else:
			self.gatewayMode()

	def startupPhaseCoT(self):
		global response
		if self.statusCoT == "STARTING":
			if self.config['deviceLogin'] == "" or self.config['devicePassword'] == "":
				self.statusCoT = "REQ_CREDENTIALS"
				self.p("CoT", "Requesting device credentials from Cloud of Things")
			else:
				self.statusCoT = "CHECK_REGISTRATION"

		elif (self.statusCoT == "REQ_CREDENTIALS" or self.statusCoT == "REQ_CREDENTIALS_REPEATED") and self.now > self.lastRequestTime + self.config['requestIntervalCoT']:
				try:
					self.lastRequestTime = self.now
					response = urequests.post("https://management.ram.m2m.telekom.com/devicecontrol/deviceCredentials", headers={"Authorization": "Basic %s" % self.authBase64default, "Content-Type": "application/vnd.com.nsn.cumulocity.deviceCredentials+json", "Accept": "application/vnd.com.nsn.cumulocity.deviceCredentials+json"}, data='{"id": "%s"}' % self.IDhex)
					responseJSON = json.loads(response.text)
					print(responseJSON)

					if "error" in responseJSON and self.statusCoT == "REQ_CREDENTIALS": # http status code could be checked for 404 as well
						if responseJSON["error"].find("Not Found") != -1:
							self.p("WARNING", "No device request for Device ID %s" % (self.IDhex))
							self.statusCoT = "REQ_CREDENTIALS_REPEATED"
						else:
							print(responseJSON)

					elif "username" and "password" and "id" in responseJSON:
						self.p("CoT", "Device ID %s username/password is %s/%s" % (responseJSON["id"], responseJSON["username"], responseJSON["password"]))
						self.config['deviceLogin'] = responseJSON["username"]
						self.config['devicePassword'] = responseJSON["password"]
						self.saveConfig()
						self.statusCoT = "CHECK_REGISTRATION"

				except Exception as e:
					self.p("WARNING", "Could not send request to Cloud of Things (%s)" % (e))
					self.CoTisConnected = False

		elif self.statusCoT == "CHECK_REGISTRATION" and self.now > self.lastRequestTime + self.config['requestIntervalCoT']:
				try:
					self.lastRequestTime = self.now
					response = urequests.get("https://iotatst.ram.m2m.telekom.com/identity/externalIds/c8y_Serial/parking_gateway-%s" % self.IDhex, headers={"Authorization": "Basic %s" % self.authBase64, "Content-Type": "application/vnd.com.nsn.cumulocity.externalID+json"})

					if response.status_code == 404:
						self.p("CoT", "Device not created & registered yet")
						self.statusCoT = "CREATE_DEVICE"
					elif response.status_code == 200:
						responseJSON = json.loads(response.text)
						self.deviceCoTID = responseJSON["managedObject"]["id"]
						self.p("CoT", "Device already created & registered with id %s" % (self.deviceCoTID))
						self.statusCoT = "UPDATE_DEVICE"

				except Exception as e:
					self.p("WARNING", "Could not send request to Cloud of Things (%s)" % (e))
					self.CoTisConnected = False

		elif self.statusCoT == "CREATE_DEVICE" and self.now > self.lastRequestTime + self.config['requestIntervalCoT']:
				try:
					self.lastRequestTime = self.now
					response = urequests.post("https://iotatst.ram.m2m.telekom.com/inventory/managedObjects", headers={"Authorization": "Basic %s" % self.authBase64, "Content-Type": "application/vnd.com.nsn.cumulocity.managedObject+json", "Accept": "application/vnd.com.nsn.cumulocity.managedObject+json"}, data='{"c8y_IsDevice": {}, "name": "Parking gateway %s"}' % self.IDhex)
					if response.status_code == 201:
						responseJSON = json.loads(response.text)
						self.deviceCoTID = responseJSON["id"]
						self.p("CoT", "Device %s created with Cloud of Things ID %s" % (responseJSON["name"], self.deviceCoTID))
						self.statusCoT = "REGISTER_DEVICE"

				except Exception as e:
					self.p("WARNING", "Could not send request to Cloud of Things (%s)" % (e))
					self.CoTisConnected = False

		elif self.statusCoT == "REGISTER_DEVICE" and self.now > self.lastRequestTime + self.config['requestIntervalCoT']:
				try:
					self.lastRequestTime = self.now
					response = urequests.post("https://iotatst.ram.m2m.telekom.com/identity/globalIds/%s/externalIds" % self.deviceCoTID, headers={"Authorization": "Basic %s" % self.authBase64, "Content-Type": "application/vnd.com.nsn.cumulocity.externalId+json", "Accept": "application/vnd.com.nsn.cumulocity.externalId+json"}, data='{"type": "c8y_Serial", "externalId": "parking_gateway-%s"}' % self.IDhex)
					if response.status_code == 201:
						responseJSON = json.loads(response.text)
						self.p("CoT", "External ID %s registered with Cloud of Things ID %s" % (responseJSON["externalId"], self.deviceCoTID))
						self.statusCoT = "STARTUP_FINISHED"
					else:
						print(response.status_code, response.text)

				except Exception as e:
					self.p("WARNING", "Could not send request to Cloud of Things (%s)" % (e))
					self.CoTisConnected = False

		elif self.statusCoT == "UPDATE_DEVICE" and self.now > self.lastRequestTime + self.config['requestIntervalCoT']:
				self.p("CoT", "Updatng device information on Cloud of Things (to be implemented...)")
				self.statusCoT = "STARTUP_FINISHED"

	def sendMeasurementToCoT(self, sensorNr, distance, voltage):
		global data
		global response
		self.rtcNow = self.rtc.now()
		#print("source: {id %s}, time: %s-%02d-%02dT%02d:%02d:%02d.%03d+00:00, type: parking_sensor, c8y_measurement: {measuredDistance-%02d: {value: %s, unit: %02d}%s}}" % (self.deviceCoTID, self.rtcNow[0], self.rtcNow[1], self.rtcNow[2], self.rtcNow[3], self.rtcNow[4], self.rtcNow[5], self.rtcNow[6] // 1000, sensorNr, distance, sensorNr, vol))
		data='{"source": {"id": "%s"}, "time": "%s-%02d-%02dT%02d:%02d:%02d.%03d+00:00", "type": "parking_sensor", "c8y_measurement": {"measuredDistance-%02d": {"value": %s, "unit": "cm"}, "batteryPercentage-%02d": {"value":%s, "unit": "per" }}}' % (self.deviceCoTID, self.rtcNow[0], self.rtcNow[1], self.rtcNow[2], self.rtcNow[3], self.rtcNow[4], self.rtcNow[5], self.rtcNow[6] // 1000, sensorNr, distance, sensorNr, voltage)
		self.p("CoT", "Sending measurement to Cloud of Things > %s" % (data))

		try:
			response = urequests.post("https://iotatst.ram.m2m.telekom.com/measurement/measurements", headers={"Authorization": "Basic %s" % self.authBase64, "Content-Type": "application/vnd.com.nsn.cumulocity.measurement+json", "Accept": "application/vnd.com.nsn.cumulocity.measurement+json"}, data=data)
			responseJSON = json.loads(response.text)
			if response.status_code != 201:
				print(response.status_code, response.text)

		except Exception as e:
			self.p("WARNING", "Could not send request to Cloud of Things (%s)" % (e))
			self.CoTisConnected = False


	def gatewayMode(self):
		self.p("INFO", "Starting in LoRa gateway mode")
		self.targetGatewayID = bytearray(6)
		self.sensorID = bytearray(6)
		self.sensorNr = 0
		self.wlan = network.WLAN(mode=network.WLAN.STA, antenna=network.WLAN.INT_ANT)
		self.statusCoT = "STARTING"
		self.lastRequestTime = 0
		self.timeSynchonized = False

		try:
			self.wlan.ifconfig(config='dhcp')
		except Exception as e:
			self.p("WARNING", e)
		self.p("WAN", "Trying to connect to WiFi SSID %s" % (self.config['WiFiSSID']))
		self.wlan.connect(ssid=self.config['WiFiSSID'], auth=(network.WLAN.WPA2, self.config['WiFiPassword']))

		try:
			self.lora = LoRa(mode=LoRa.LORA, frequency=self.config['frequency'], tx_power=int(self.config['powerTX']), bandwidth=self.config['bandwidth'], sf=self.config['spreadingFactor'], coding_rate=self.config['codingRate'], rx_iq=True)
			self.p("INFO", "LoRa radio interface initialized at %sHz with %sdBm TX power, %skHz bandwidth, spreading factor %s and coding rate %s" % (self.config['frequency'], self.config['powerTX'], BANDWIDTH[self.config['bandwidth']], self.config['spreadingFactor'], CODING_RATE[self.config['codingRate']-1]))
		except Exception as e:
			self.p("WARNING", "Error during Lora radio interface initialization: %s" % e)

		self.lora_sock = socket.socket(socket.AF_LORA, socket.SOCK_RAW)
		self.lora_sock.setblocking(False)

		while True:
			self.now = time.time()

			self.WiFiIsConnected = self.wlan.isconnected()

			if self.WiFiIsConnected and not self.WiFiIsConnectedLast:
				self.p("WAN", "Connected to WiFi SSID %s (IP address %s)" % (self.config['WiFiSSID'], self.wlan.ifconfig()[0]))

			if not self.WiFiIsConnected and self.WiFiIsConnectedLast:
				self.p("WAN", "Disconnected from WiFi SSID %s" % (self.config['WiFiSSID']))

			self.WiFiIsConnectedLast = self.WiFiIsConnected

			if self.WiFiIsConnected:

				if not self.timeSynchonized:
					try:
						self.rtc.ntp_sync("pool.ntp.org")
						self.p("INFO", "Time synchronized")
						self.timeSynchonized = True
					except:
						self.p("WARNING", "Could not synchronize time")

				if self.statusCoT != "STARTUP_FINISHED":
					self.startupPhaseCoT()

			encryptedPkg = self.lora_sock.recv(512)
			if len(encryptedPkg) > 0:
				if len(encryptedPkg) == 31:
					cipher = crypto.AES(self.config["AESkey"], crypto.AES.MODE_CFB, encryptedPkg[:16])
					recv_pkg = cipher.decrypt(encryptedPkg[16:])

					self.sensorNr, self.voltage, self.distance, self.targetGatewayID[0], self.targetGatewayID[1], self.targetGatewayID[2], self.targetGatewayID[3], self.targetGatewayID[4], self.targetGatewayID[5], self.sensorID[0], self.sensorID[1], self.sensorID[2], self.sensorID[3], self.sensorID[4], self.sensorID[5] = struct.unpack(_LORA_PKG_FORMAT, recv_pkg)

					if self.IDhex == str(binascii.hexlify(self.targetGatewayID))[2:-1]:
						self.p("INFO", "Sensor Nr:%s Sensor ID:%s Target GW:%s > Voltage:%sV Distance:%scm RSSI:%sdBm SNR:%sdB" % (self.sensorNr, str(binascii.hexlify(self.sensorID))[2:-1], str(binascii.hexlify(self.targetGatewayID))[2:-1], self.voltage, self.distance, self.lora.stats()[1], self.lora.stats()[2]))

						if self.statusCoT == "STARTUP_FINISHED":
							self.sendMeasurementToCoT(self.sensorNr, self.distance, self.voltage)

				else:
					self.p("WARNING", "Unexpected lenght of LoRa message: %sbytes (%s)" % (len(encryptedPkg), encryptedPkg))

			gc.collect()

node = LoRaNode(i2c, rtc)
node.run()

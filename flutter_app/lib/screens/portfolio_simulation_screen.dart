import 'package:flutter/material.dart';
import 'package:fl_chart/fl_chart.dart';
import '../services/api_service.dart';
import '../services/user_service.dart';

class PortfolioSimulationScreen extends StatefulWidget {
  const PortfolioSimulationScreen({super.key});

  @override
  State<PortfolioSimulationScreen> createState() => _PortfolioSimulationScreenState();
}

class _PortfolioSimulationScreenState extends State<PortfolioSimulationScreen> {
  final _formKey = GlobalKey<FormState>();
  double _monthlyInvestment = 200;
  int _years = 10;
  double _stocksPercent = 70;
  double _bondsPercent = 30;
  
  Map<String, dynamic>? _simulationResult;
  bool _isLoading = false;

  @override
  Widget build(BuildContext context) {
    return Scaffold(
      appBar: AppBar(
        title: const Text('Portfolio Simulator'),
        backgroundColor: Colors.blue[700],
        foregroundColor: Colors.white,
      ),
      body: SingleChildScrollView(
        padding: const EdgeInsets.all(24),
        child: Form(
          key: _formKey,
          child: Column(
            crossAxisAlignment: CrossAxisAlignment.start,
            children: [
              const Text(
                'See how your investments could grow',
                style: TextStyle(
                  fontSize: 24,
                  fontWeight: FontWeight.bold,
                ),
              ),
              const SizedBox(height: 8),
              Text(
                'Based on historical market data',
                style: TextStyle(color: Colors.grey[600]),
              ),
              const SizedBox(height: 32),
              _buildMonthlyInvestmentSlider(),
              const SizedBox(height: 24),
              _buildYearsSelector(),
              const SizedBox(height: 24),
              _buildAssetAllocation(),
              const SizedBox(height: 32),
              SizedBox(
                width: double.infinity,
                height: 50,
                child: ElevatedButton(
                  onPressed: _isLoading ? null : _runSimulation,
                  style: ElevatedButton.styleFrom(
                    backgroundColor: Colors.blue[700],
                    foregroundColor: Colors.white,
                  ),
                  child: _isLoading
                      ? const CircularProgressIndicator(color: Colors.white)
                      : const Text('Run Simulation'),
                ),
              ),
              if (_simulationResult != null) ...[
                const SizedBox(height: 32),
                _buildResults(),
              ],
            ],
          ),
        ),
      ),
    );
  }

  Widget _buildMonthlyInvestmentSlider() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Text(
          'Monthly Investment: \$${_monthlyInvestment.toInt()}',
          style: const TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.bold,
          ),
        ),
        Slider(
          value: _monthlyInvestment,
          min: 50,
          max: 2000,
          divisions: 39,
          label: '\$${_monthlyInvestment.toInt()}',
          onChanged: (value) => setState(() => _monthlyInvestment = value),
        ),
      ],
    );
  }

  Widget _buildYearsSelector() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Time Horizon',
          style: TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 12),
        Wrap(
          spacing: 12,
          runSpacing: 12,
          children: [5, 10, 15, 20, 25, 30].map((years) {
            final isSelected = _years == years;
            return ChoiceChip(
              label: Text('$years years'),
              selected: isSelected,
              onSelected: (selected) {
                if (selected) setState(() => _years = years);
              },
            );
          }).toList(),
        ),
      ],
    );
  }

  Widget _buildAssetAllocation() {
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Asset Allocation',
          style: TextStyle(
            fontSize: 18,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 16),
        Text('Stocks: ${_stocksPercent.toInt()}%'),
        Slider(
          value: _stocksPercent,
          min: 0,
          max: 100,
          divisions: 100,
          label: '${_stocksPercent.toInt()}%',
          onChanged: (value) {
            setState(() {
              _stocksPercent = value;
              _bondsPercent = 100 - value;
            });
          },
        ),
        Text('Bonds: ${_bondsPercent.toInt()}%'),
        Slider(
          value: _bondsPercent,
          min: 0,
          max: 100,
          divisions: 100,
          label: '${_bondsPercent.toInt()}%',
          onChanged: (value) {
            setState(() {
              _bondsPercent = value;
              _stocksPercent = 100 - value;
            });
          },
        ),
      ],
    );
  }

  Future<void> _runSimulation() async {
    setState(() {
      _isLoading = true;
      _simulationResult = null;
    });

    try {
      final userId = await UserService.getUserId();
      if (userId == null) {
        throw Exception('User not found');
      }

      final response = await ApiService.simulatePortfolio({
        'user_id': userId,
        'monthly_investment': _monthlyInvestment,
        'years': _years,
        'asset_allocation': {
          'stocks': _stocksPercent / 100,
          'bonds': _bondsPercent / 100,
        },
      });

      setState(() {
        _simulationResult = response['simulation'];
        _isLoading = false;
      });
    } catch (e) {
      setState(() => _isLoading = false);
      if (mounted) {
        ScaffoldMessenger.of(context).showSnackBar(
          SnackBar(content: Text('Error: $e')),
        );
      }
    }
  }

  Widget _buildResults() {
    if (_simulationResult == null) return const SizedBox();

    final summary = _simulationResult!['summary'] as Map<String, dynamic>;
    final totalInvested = summary['total_invested']?.toDouble() ?? 0.0;
    final finalValue = summary['final_value']?.toDouble() ?? 0.0;
    final totalReturn = summary['total_return']?.toDouble() ?? 0.0;
    final returnPercentage = summary['return_percentage']?.toDouble() ?? 0.0;

    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        const Text(
          'Simulation Results',
          style: TextStyle(
            fontSize: 24,
            fontWeight: FontWeight.bold,
          ),
        ),
        const SizedBox(height: 24),
        _buildResultCard(
          'Total Invested',
          '\$${totalInvested.toStringAsFixed(2)}',
          Icons.account_balance_wallet,
          Colors.blue,
        ),
        const SizedBox(height: 16),
        _buildResultCard(
          'Final Value',
          '\$${finalValue.toStringAsFixed(2)}',
          Icons.trending_up,
          Colors.green,
        ),
        const SizedBox(height: 16),
        _buildResultCard(
          'Total Return',
          '\$${totalReturn.toStringAsFixed(2)} (${returnPercentage.toStringAsFixed(2)}%)',
          Icons.attach_money,
          returnPercentage >= 0 ? Colors.green : Colors.red,
        ),
        const SizedBox(height: 32),
        if (_simulationResult!['monthly_values'] != null)
          _buildChart(),
      ],
    );
  }

  Widget _buildResultCard(String label, String value, IconData icon, Color color) {
    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Row(
          children: [
            Container(
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                color: color.withOpacity(0.2),
                shape: BoxShape.circle,
              ),
              child: Icon(icon, color: color),
            ),
            const SizedBox(width: 16),
            Expanded(
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                mainAxisSize: MainAxisSize.min,
                children: [
                  Text(
                    label,
                    style: TextStyle(
                      color: Colors.grey[600],
                      fontSize: 14,
                    ),
                  ),
                  const SizedBox(height: 4),
                  Text(
                    value,
                    style: const TextStyle(
                      fontSize: 18,
                      fontWeight: FontWeight.bold,
                    ),
                    overflow: TextOverflow.ellipsis,
                  ),
                ],
              ),
            ),
          ],
        ),
      ),
    );
  }

  Widget _buildChart() {
    final monthlyValues = _simulationResult!['monthly_values'] as List;
    if (monthlyValues.isEmpty) return const SizedBox();

    final spots = monthlyValues.asMap().entries.map((entry) {
      final month = entry.key.toDouble();
      final value = (entry.value['value'] as num).toDouble();
      return FlSpot(month, value);
    }).toList();

    return Card(
      child: Padding(
        padding: const EdgeInsets.all(16),
        child: Column(
          crossAxisAlignment: CrossAxisAlignment.start,
          children: [
            const Text(
              'Growth Over Time',
              style: TextStyle(
                fontSize: 18,
                fontWeight: FontWeight.bold,
              ),
            ),
            const SizedBox(height: 16),
            SizedBox(
              height: 200,
              child: LineChart(
                LineChartData(
                  gridData: FlGridData(show: true),
                  titlesData: FlTitlesData(show: false),
                  borderData: FlBorderData(show: true),
                  lineBarsData: [
                    LineChartBarData(
                      spots: spots,
                      isCurved: true,
                      color: Colors.blue,
                      barWidth: 3,
                      dotData: FlDotData(show: false),
                      belowBarData: BarAreaData(show: true, color: Colors.blue.withOpacity(0.1)),
                    ),
                  ],
                ),
              ),
            ),
          ],
        ),
      ),
    );
  }
}

